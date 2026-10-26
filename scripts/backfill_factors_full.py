"""隔离全量重算 factor_scores，具备 PIT、失败传播、checkpoint 与 provenance 门禁。

本工具只允许写入独立目标库。首次运行从只读源库创建 SQLite 完整副本；恢复运行时
校验源库指纹和运行配置，不会写入或迁移源库。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def emit(log_path: Path, event: str, **payload) -> None:
    record = {"event": event, "timestamp": utc_now(), **payload}
    line = json.dumps(record, ensure_ascii=False, sort_keys=True)
    print(line, flush=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def source_fingerprint(path: Path) -> str:
    """Return a content fingerprint so resume safety does not depend on metadata."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def config_hash(config: dict) -> str:
    raw = json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def git_commit(repo: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo, stderr=subprocess.DEVNULL, text=True
        ).strip()
    except Exception:
        return "unknown"


def create_isolated_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    source_uri = f"file:{source.as_posix()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    os.chmod(target, 0o600)


def ensure_control_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS factor_recompute_runs (
            run_id TEXT PRIMARY KEY,
            source_path TEXT NOT NULL,
            source_fingerprint TEXT NOT NULL,
            config_hash TEXT NOT NULL,
            code_commit TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            planned_dates INTEGER NOT NULL DEFAULT 0,
            completed_dates INTEGER NOT NULL DEFAULT 0,
            rows_written INTEGER NOT NULL DEFAULT 0,
            failure_count INTEGER NOT NULL DEFAULT 0,
            quick_check TEXT
        );
        CREATE TABLE IF NOT EXISTS factor_recompute_checkpoints (
            config_hash TEXT NOT NULL,
            date TEXT NOT NULL,
            status TEXT NOT NULL,
            rows_written INTEGER NOT NULL DEFAULT 0,
            completed_at TEXT NOT NULL,
            PRIMARY KEY (config_hash, date)
        );
        CREATE TABLE IF NOT EXISTS factor_recompute_failures (
            run_id TEXT NOT NULL,
            date TEXT NOT NULL,
            error_type TEXT NOT NULL,
            error_message TEXT NOT NULL,
            failed_at TEXT NOT NULL,
            PRIMARY KEY (run_id, date)
        );
        CREATE TABLE IF NOT EXISTS factor_recompute_provenance (
            run_id TEXT NOT NULL,
            date TEXT NOT NULL,
            code TEXT NOT NULL,
            factor_count INTEGER NOT NULL,
            factor_hash TEXT NOT NULL,
            computed_at TEXT NOT NULL,
            PRIMARY KEY (run_id, date, code)
        );
        """
    )
    conn.commit()


def validate_pit(conn: sqlite3.Connection, date: str) -> None:
    """拒绝缺失日期及任何已记录的计算日之后读取。"""
    trace_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_read_trace'"
    ).fetchone()
    if trace_exists:
        future_reads = conn.execute(
            "SELECT table_name, max_date_read FROM audit_read_trace "
            "WHERE requested_as_of=? AND max_date_read>? ORDER BY table_name LIMIT 1",
            (date, date),
        ).fetchone()
        if future_reads:
            raise RuntimeError(
                f"PIT gate failed: future read from {future_reads[0]} "
                f"for {date}: {future_reads[1]}"
            )
    for table in (
        "daily_klines",
        "stock_financials",
        "stock_valuation",
        "stock_moneyflow",
        "concept_daily",
        "lhb_daily",
        "analyst_forecasts",
        "market_sentiment",
    ):
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            continue
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if "date" not in columns:
            continue
        # This is a dataset-level diagnostic. The actual computation path must query <= date.
        invalid = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE date IS NULL OR date = ''"
        ).fetchone()[0]
        if invalid:
            raise RuntimeError(f"PIT gate failed: {table} contains {invalid} missing dates")


def production_processor(db_path: Path, date: str) -> list[tuple[str, str]]:
    """返回 (code, canonical factor_scores JSON)，异常必须向调用方传播。"""
    from common.database import Database
    from data_layer import DataLayer
    from strategy_layer import StrategyLayer
    from strategy_layer.aggregation.aggregator import Aggregator, DEFAULT_WEIGHTS

    db = Database(str(db_path))
    try:
        data_layer = DataLayer(db)
        strategy = StrategyLayer(data_layer=data_layer, db=db, mode="backtest")
        codes = strategy._get_stock_codes(date)
        if not codes:
            raise RuntimeError(f"no eligible stock codes for {date}")
        matrix = strategy.factor_engine.compute_all(codes, date)
        rows = Aggregator().aggregate(matrix, weights=DEFAULT_WEIGHTS)
        if len(rows) != len(codes):
            raise RuntimeError(
                f"factor coverage mismatch for {date}: expected={len(codes)} actual={len(rows)}"
            )
        result = []
        for row in rows:
            if not row.factor_scores:
                raise RuntimeError(f"empty factor_scores for {row.code} on {date}")
            if any(not math.isfinite(float(value)) for value in row.factor_scores.values()):
                raise RuntimeError(f"non-finite factor score for {row.code} on {date}")
            result.append((row.code, json.dumps(row.factor_scores, sort_keys=True, separators=(",", ":"))))
        return result
    finally:
        db.close()


def run(
    source: Path,
    target: Path,
    log_path: Path,
    start_date: str | None = None,
    end_date: str | None = None,
    processor: Callable[[Path, str], list[tuple[str, str]]] = production_processor,
    repo: Path | None = None,
) -> dict:
    source = source.resolve()
    target = target.resolve()
    log_path = log_path.resolve()
    if source == target:
        raise ValueError("source and target must be different; formal database writes are forbidden")
    if target.name == "qs.db" or target.parent == source.parent:
        raise ValueError("target must be an isolated database outside the source directory")
    if not source.is_file():
        raise FileNotFoundError(source)
    if not target.exists():
        create_isolated_copy(source, target)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    fingerprint = source_fingerprint(source)
    cfg = {"start_date": start_date, "end_date": end_date, "algorithm": "factor_scores_full_v1"}
    cfg_hash = config_hash(cfg)
    run_id = uuid.uuid4().hex
    code_commit = git_commit(repo or Path(__file__).resolve().parents[1])
    conn = sqlite3.connect(target)
    try:
        ensure_control_tables(conn)
        prior = conn.execute(
            "SELECT DISTINCT source_fingerprint FROM factor_recompute_runs"
        ).fetchall()
        if any(row[0] != fingerprint for row in prior):
            raise RuntimeError("resume gate failed: source database fingerprint changed")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(factor_daily)")}
        if "factor_scores" not in columns:
            raise RuntimeError("factor_daily.factor_scores is absent; migration is not allowed here")
        clauses = ["1=1"]
        params: list[str] = []
        if start_date:
            clauses.append("date >= ?")
            params.append(start_date)
        if end_date:
            clauses.append("date <= ?")
            params.append(end_date)
        dates = [
            row[0]
            for row in conn.execute(
                f"SELECT DISTINCT date FROM factor_daily WHERE {' AND '.join(clauses)} ORDER BY date",
                params,
            )
        ]
        completed = {
            row[0]
            for row in conn.execute(
                "SELECT date FROM factor_recompute_checkpoints WHERE config_hash=? AND status='completed'",
                (cfg_hash,),
            )
        }
        pending = [date for date in dates if date not in completed]
        conn.execute(
            "INSERT INTO factor_recompute_runs "
            "(run_id,source_path,source_fingerprint,config_hash,code_commit,started_at,status,planned_dates) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (run_id, str(source), fingerprint, cfg_hash, code_commit, utc_now(), "running", len(dates)),
        )
        conn.commit()
        emit(log_path, "run_started", run_id=run_id, planned_dates=len(dates), pending_dates=len(pending))
        completed_dates = rows_written = failures = 0
        for date in pending:
            started = time.monotonic()
            try:
                validate_pit(conn, date)
                rows = processor(target, date)
                if not rows:
                    raise RuntimeError(f"processor produced no rows for {date}")
                codes = [code for code, _scores_json in rows]
                if len(codes) != len(set(codes)):
                    raise RuntimeError(f"processor produced duplicate codes for {date}")
                expected_codes = {
                    row[0]
                    for row in conn.execute(
                        "SELECT code FROM factor_daily WHERE date=?", (date,)
                    )
                }
                if not set(codes).issubset(expected_codes):
                    raise RuntimeError(
                        f"factor coverage mismatch for {date}: "
                        f"produced {len(set(codes) - expected_codes)} codes absent from factor_daily "
                        f"(expected_pool={len(expected_codes)} actual={len(codes)})"
                    )
                conn.execute("BEGIN IMMEDIATE")
                for code, scores_json in rows:
                    decoded = json.loads(scores_json)
                    if not isinstance(decoded, dict) or not decoded:
                        raise RuntimeError(f"empty or invalid factor_scores for {code} on {date}")
                    if any(not math.isfinite(float(value)) for value in decoded.values()):
                        raise RuntimeError(f"non-finite factor score for {code} on {date}")
                    canonical_scores = json.dumps(
                        decoded, sort_keys=True, separators=(",", ":")
                    )
                    digest = hashlib.sha256(canonical_scores.encode()).hexdigest()
                    updated = conn.execute(
                        "UPDATE factor_daily SET factor_scores=? WHERE code=? AND date=?",
                        (canonical_scores, code, date),
                    ).rowcount
                    if updated != 1:
                        raise RuntimeError(f"missing factor_daily row for {code} on {date}")
                    conn.execute(
                        "INSERT INTO factor_recompute_provenance VALUES (?,?,?,?,?,?)",
                        (run_id, date, code, len(decoded), digest, utc_now()),
                    )
                conn.execute(
                    "INSERT OR REPLACE INTO factor_recompute_checkpoints VALUES (?,?,?,?,?)",
                    (cfg_hash, date, "completed", len(rows), utc_now()),
                )
                conn.commit()
                completed_dates += 1
                rows_written += len(rows)
                emit(log_path, "date_completed", run_id=run_id, date=date, rows=len(rows), elapsed_s=round(time.monotonic()-started, 3))
            except Exception as exc:
                conn.rollback()
                failures += 1
                conn.execute(
                    "INSERT OR REPLACE INTO factor_recompute_failures VALUES (?,?,?,?,?)",
                    (run_id, date, type(exc).__name__, str(exc), utc_now()),
                )
                conn.commit()
                emit(log_path, "date_failed", run_id=run_id, date=date, error_type=type(exc).__name__, message=str(exc))
        quick_check = conn.execute("PRAGMA quick_check").fetchone()[0]
        provenance_rows = conn.execute(
            "SELECT COUNT(*) FROM factor_recompute_provenance WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        status = "quality_pass" if failures == 0 and provenance_rows == rows_written and quick_check == "ok" else "failed"
        conn.execute(
            "UPDATE factor_recompute_runs SET finished_at=?,status=?,completed_dates=?,rows_written=?,failure_count=?,quick_check=? WHERE run_id=?",
            (utc_now(), status, completed_dates, rows_written, failures, quick_check, run_id),
        )
        conn.commit()
        summary = {"run_id": run_id, "status": status, "planned_dates": len(dates), "already_completed": len(completed), "completed_dates": completed_dates, "rows_written": rows_written, "failures": failures, "provenance_rows": provenance_rows, "quick_check": quick_check}
        emit(log_path, "summary", **summary)
        return summary
    finally:
        conn.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        summary = run(args.source, args.target, args.log, args.start_date, args.end_date)
    except Exception as exc:
        print(json.dumps({"event": "fatal", "error_type": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0 if summary["status"] == "quality_pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
