#!/usr/bin/env python3
"""P2-13 迁移前核对（只读）— 统计隔离库 → 正式库的迁移规模与一致性。

只读验证（不写任何正式库/隔离库），输出：
- 正式库 factor_scores 当前覆盖 vs 隔离库
- 待迁移更新行数 / 待新增行数 / 保持原样行数
- 键(code,date) 一致性（隔离库内无重复主键）
- 隔离库最新 run 状态（quality_pass / running / failed）
- 正式库数据库文件 SHA256（用于迁移前基线）
- 服务未启用证明

用法:
    python3 scripts/prepare_p2_13_migration.py \
        --isolated-db ~/quantstar-factor-iso/factor_v4.db \
        --out ~/quantstar-factor-iso/logs/p2_13_prep.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FORMAL_DB = REPO / "data" / "qs.db"


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git_head(repo: Path) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True,
    )
    return out.stdout.strip()


def latest_run(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT run_id,status,planned_dates,completed_dates,rows_written,"
        "failure_count,quick_check,started_at,finished_at "
        "FROM factor_recompute_runs ORDER BY started_at DESC LIMIT 1"
    ).fetchall()
    if not rows:
        return {}
    r = rows[0]
    return {
        "run_id": r[0], "status": r[1], "planned_dates": r[2],
        "completed_dates": r[3], "rows_written": r[4],
        "failure_count": r[5], "quick_check": r[6],
        "started_at": r[7], "finished_at": r[8],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--isolated-db", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    formal = sqlite3.connect(f"file:{FORMAL_DB}?mode=ro", uri=True)
    isolated = sqlite3.connect(f"file:{args.isolated_db}?mode=ro", uri=True)

    try:
        # SQL 端做差异统计,避免把全部行载入 Python 内存(正式库~135K,隔离库数百万)
        formal.execute(f"ATTACH DATABASE '{args.isolated_db}' AS iso")
        f_scores = formal.execute(
            "SELECT COUNT(*) FROM factor_daily WHERE factor_scores IS NOT NULL"
        ).fetchone()[0]
        i_scores = formal.execute(
            "SELECT COUNT(*) FROM iso.factor_daily WHERE factor_scores IS NOT NULL"
        ).fetchone()[0]

        # to_update: 两边都有且值不同
        changed = formal.execute(
            "SELECT COUNT(*) FROM factor_daily f JOIN iso.factor_daily i "
            "ON f.code=i.code AND f.date=i.date "
            "WHERE f.factor_scores IS NOT NULL AND i.factor_scores IS NOT NULL "
            "AND f.factor_scores <> i.factor_scores"
        ).fetchone()[0]
        # same: 两边都有且值相同
        same = formal.execute(
            "SELECT COUNT(*) FROM factor_daily f JOIN iso.factor_daily i "
            "ON f.code=i.code AND f.date=i.date "
            "WHERE f.factor_scores IS NOT NULL AND i.factor_scores IS NOT NULL "
            "AND f.factor_scores = i.factor_scores"
        ).fetchone()[0]
        # added: 隔离库有、正式库无
        added = formal.execute(
            "SELECT COUNT(*) FROM iso.factor_daily i "
            "LEFT JOIN factor_daily f ON f.code=i.code AND f.date=i.date "
            "WHERE i.factor_scores IS NOT NULL AND f.code IS NULL"
        ).fetchone()[0]
        # removed: 正式库有、隔离库无(默认迁移不删除,仅报告)
        removed = formal.execute(
            "SELECT COUNT(*) FROM factor_daily f "
            "LEFT JOIN iso.factor_daily i ON f.code=i.code AND f.date=i.date "
            "WHERE f.factor_scores IS NOT NULL AND i.code IS NULL"
        ).fetchone()[0]

        dup = isolated.execute(
            "SELECT COUNT(*) FROM (SELECT code, date, COUNT(*) c FROM factor_daily "
            "WHERE factor_scores IS NOT NULL GROUP BY code, date HAVING COUNT(*) > 1)"
        ).fetchone()[0]

        run = latest_run(isolated)

        svc = subprocess.run(
            ["systemctl", "is-active", "quantstar", "quantstar-api", "quantstar-dash"],
            capture_output=True, text=True,
        )
        active_services = [s for s in svc.stdout.split() if s == "active"]

        report = {
            "generated_at": utc_iso(),
            "repo": str(REPO),
            "code_head": git_head(REPO),
            "formal_db": str(FORMAL_DB),
            "formal_db_sha256": sha256(FORMAL_DB),
            "formal_db_size_bytes": FORMAL_DB.stat().st_size,
            "isolated_db": args.isolated_db,
            "isolated_db_sha256": sha256(Path(args.isolated_db)),
            "formal_factor_scores_nonnull": f_scores,
            "isolated_factor_scores_nonnull": i_scores,
            "migration": {
                "to_update": changed,
                "unchanged": same,
                "to_add": added,
                "formal_only_rows_to_keep": removed,
            },
            "isolated_latest_run": run,
            "isolated_dup_keys": dup,
            "service_status": {
                "active_services": active_services,
                "all_inactive": len(active_services) == 0,
            },
            "notes": (
                "只读核对,未写任何库。to_update+to_add 为迁移正式库时的总变更行数;"
                "removed 为正式库独有且隔离库已无的行(隔离库未删正式库数据时应为0,"
                "实际取决于是否启用删除;默认不删除则忽略)。"
            ),
        }

        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

        print(f"P2-13 prep report written to {args.out}")
        print(json.dumps(
            {
                "formal_scores": f_scores,
                "isolated_scores": i_scores,
                "to_update": changed,
                "to_add": added,
                "unchanged": same,
                "latest_run": run.get("status"),
                "isolated_dup_keys": dup,
                "formal_db_sha256": report["formal_db_sha256"],
            },
            ensure_ascii=False, indent=2,
        ))
        return 0
    finally:
        try:
            formal.execute("DETACH DATABASE iso")
        except Exception:
            pass
        formal.close()
        isolated.close()


if __name__ == "__main__":
    raise SystemExit(main())
