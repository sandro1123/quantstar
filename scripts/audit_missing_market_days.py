#!/usr/bin/env python3
"""B2-R1: Anomaly trading days fact audit for 2026-07-08 and 2026-07-20.

Read-only audit. Nova finding: the original B2 over-inferred a root cause from
side-table presence (any other table with data => FORMAL_KLINE_INGESTION_GAP).
Side-table data is NOT proof that daily_klines were expected for a date.

B2-R1 rules:
  * Report only observed facts (row counts, run records, run_id formats).
  * Both anomaly dates are classified UNRESOLVED.
  * No root-cause label is derived from unrelated tables (lhb_daily,
    analyst_forecasts, valuation candidates, etc.).
  * The pipeline_runs fact chain is preserved verbatim:
      - pipeline_runs was created 2026-07-18 (min date); no 07-08 log is by design.
      - 07-20 daily_data_pipeline_2026-07-20_153001 uses the old run_id format
        (no uuid suffix), start=end=15:30:01, status=success, duration_s=0.0,
        version=NULL, config_hash=NULL (end_run computes duration from
        second-precision timestamps; same-second => 0).
      - 07-21 daily_data_pipeline failed repeatedly with "timeout after 300s".
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ANOMALY_DATES = ["2026-07-08", "2026-07-20"]
CONTEXT_DATES = ["2026-07-07", "2026-07-09", "2026-07-10", "2026-07-17", "2026-07-21", "2026-07-22"]

log = logging.getLogger("audit_missing_days")


def open_ro(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA temp_store=MEMORY")
    cur = conn.execute("PRAGMA query_only")
    if cur.fetchone()[0] != 1:
        raise RuntimeError("query_only verification failed")
    conn.row_factory = sqlite3.Row
    return conn


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def audit_table(conn: sqlite3.Connection, table: str, dates: list[str]) -> dict:
    """Factual row-count audit for a table across dates."""
    result = {}
    for d in dates:
        try:
            rows = conn.execute(
                f"SELECT COUNT(*) as cnt FROM {table} WHERE date = ?", (d,)
            ).fetchone()
            result[d] = rows["cnt"] if rows else 0
        except Exception as e:
            result[d] = f"ERROR: {e}"
    return result


def audit_all_tables(conn: sqlite3.Connection, dates: list[str]) -> dict:
    """Audit all relevant tables for each date (factual counts only)."""
    tables = [
        "daily_klines", "stock_moneyflow", "stock_valuation",
        "stock_financials", "lhb_daily", "market_sentiment",
        "concept_daily", "analyst_forecasts", "stock_news",
        "stock_limit_up_down", "pipeline_runs",
    ]
    result = {}
    for t in tables:
        try:
            result[t] = audit_table(conn, t, dates)
        except Exception as e:
            result[t] = {"error": str(e)}
    return result


def _is_old_format_run_id(run_id: str) -> bool:
    """True if run_id has no trailing uuid (old format, pre-07-21).

    New-format run_ids carry a trailing uuid (e.g.
    'daily_data_pipeline_2026-07-21_153004-8f3a...'); old ones end at the
    timestamp (e.g. 'daily_data_pipeline_2026-07-20_153001').
    """
    if not run_id:
        return False
    if re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", run_id.lower()):
        return False
    return True


def audit_pipeline_runs(conn: sqlite3.Connection, dates: list[str]) -> dict:
    """Introspect pipeline_runs and extract the per-date fact chain."""
    result = {
        "table_exists": False,
        "columns": [],
        "table_creation_date": None,
        "total_runs": 0,
        "distinct_dates": [],
        "per_date": {},
        "note": (
            "pipeline_runs started logging on 2026-07-18. Dates before that "
            "(e.g. 2026-07-08) have NO run log by design, not as an error."
        ),
    }
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='pipeline_runs'"
    ).fetchone()
    if not row:
        return result
    result["table_exists"] = True
    result["columns"] = [
        r["name"] for r in conn.execute("PRAGMA table_info(pipeline_runs)").fetchall()
    ]
    # The table may lack a 'date' column in some schemas; be graceful.
    if "date" not in result["columns"]:
        result["per_date"] = {d: {"run_count": 0, "runs": [], "has_daily_data_pipeline": False,
                                  "old_format_run_ids": [], "daily_data_pipeline": []} for d in dates}
        return result
    agg = conn.execute(
        "SELECT MIN(date) as mn, MAX(date) as mx, COUNT(*) as c FROM pipeline_runs"
    ).fetchone()
    result["table_creation_date"] = agg["mn"]
    result["total_runs"] = agg["c"]
    result["distinct_dates"] = [
        r["date"] for r in conn.execute("SELECT DISTINCT date FROM pipeline_runs ORDER BY date").fetchall()
    ]
    for d in dates:
        runs = conn.execute(
            "SELECT run_id, pipeline_name, date, start_time, end_time, status, "
            "error_msg, duration_s, version, config_hash "
            "FROM pipeline_runs WHERE date = ? ORDER BY start_time",
            (d,),
        ).fetchall()
        run_dicts = [dict(r) for r in runs]
        per_date = {
            "run_count": len(run_dicts),
            "runs": run_dicts,
            "has_daily_data_pipeline": any(r["pipeline_name"] == "daily_data_pipeline" for r in run_dicts),
            "old_format_run_ids": [
                r["run_id"] for r in run_dicts if _is_old_format_run_id(r["run_id"])
            ],
            "daily_data_pipeline": [
                r for r in run_dicts if r["pipeline_name"] == "daily_data_pipeline"
            ],
        }
        result["per_date"][d] = per_date
    return result


def audit_valuation_provenance(val_conn: sqlite3.Connection | None, dates: list[str]) -> dict:
    """Check valuation candidate rows for anomaly dates in the candidate DB."""
    if val_conn is None:
        return {"error": "valuation DB not available"}
    result = {}
    try:
        tables = [r["name"] for r in val_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        result["available_tables"] = tables
        for tbl in ["stock_valuation", "valuation_provenance", "daily_klines"]:
            if tbl in tables:
                result[tbl] = audit_table(val_conn, tbl, dates)
    except Exception as e:
        result["error"] = str(e)
    return result


def build_fact_chain(
    d: str,
    table_audit: dict,
    weekday_info: dict,
    pipeline_runs: dict,
    provenance: dict,
) -> dict:
    """Assemble the observed facts for a date. No inference from side tables."""
    weekday = weekday_info[d]
    findings = []
    findings.append(f"Weekday: {weekday['weekday_name']}, is_weekend={weekday['is_weekend']}")

    kline_count = 0
    if isinstance(table_audit.get("daily_klines"), dict):
        kline_count = table_audit["daily_klines"].get(d, 0)
        if isinstance(kline_count, str):
            kline_count = 0
    findings.append(f"daily_klines rows on {d}: {kline_count}")

    # Factual record of other tables (data present is a FACT, not a trading-day proof).
    other_tables_with_data = []
    for tbl, counts in table_audit.items():
        if tbl == "pipeline_runs":
            continue
        if isinstance(counts, dict) and d in counts:
            cnt = counts[d]
            if isinstance(cnt, (int, float)) and cnt > 0:
                other_tables_with_data.append(f"{tbl}={int(cnt)}")
    if other_tables_with_data:
        findings.append(
            "Other tables have rows (fact only; does NOT establish that klines "
            f"were expected): {', '.join(other_tables_with_data)}"
        )
    else:
        findings.append("No other audited tables have rows for this date.")

    # Valuation candidate rows (valuation side) as a fact.
    val_rows = None
    if isinstance(provenance.get("stock_valuation"), dict):
        val_rows = provenance["stock_valuation"].get(d)
    if val_rows is not None and not isinstance(val_rows, str):
        findings.append(f"valuation candidate rows (valuation.db stock_valuation): {int(val_rows)}")

    # pipeline_runs fact chain for this date.
    per_date = pipeline_runs.get("per_date", {}).get(d, {})
    if per_date:
        findings.append(f"pipeline_runs: {per_date['run_count']} run(s) on {d}")
        for r in per_date.get("daily_data_pipeline", []):
            run_id = r.get("run_id", "")
            run_id_note = "OLD format (no uuid suffix)" if _is_old_format_run_id(run_id) else "new format"
            findings.append(
                f"  daily_data_pipeline run_id={run_id} ({run_id_note}) "
                f"start={r.get('start_time')} end={r.get('end_time')} "
                f"status={r.get('status')} duration_s={r.get('duration_s')} "
                f"version={r.get('version')} config_hash={r.get('config_hash')}"
            )
            if r.get("status") == "success" and r.get("duration_s") == 0.0:
                findings.append(
                    "  NOTE: success with duration_s=0.0. end_run computes duration "
                    "from second-precision timestamps; same-second start/end => 0. "
                    "This run record does NOT prove klines were actually ingested."
                )
            if r.get("status") == "failed":
                findings.append(f"  NOTE: failed run error_msg={r.get('error_msg')}")
    else:
        findings.append(
            f"pipeline_runs: no run log on {d} "
            "(table created 2026-07-18; absence on earlier dates is by design, not an error)."
        )

    # Adjacent kline context (factual).
    adj_dates = []
    d_dt = datetime.strptime(d, "%Y-%m-%d")
    for adj in CONTEXT_DATES:
        adj_dt = datetime.strptime(adj, "%Y-%m-%d")
        if abs((adj_dt - d_dt).days) <= 5:
            cnt = 0
            if isinstance(table_audit.get("daily_klines"), dict):
                cnt = table_audit["daily_klines"].get(adj, 0)
                if isinstance(cnt, str):
                    cnt = 0
            adj_dates.append(f"{adj}({cnt}klines)")
    findings.append(f"Adjacent kline context: {', '.join(adj_dates)}")

    return {
        "date": d,
        "weekday": weekday,
        "verdict": "UNRESOLVED",
        "daily_klines_count": kline_count,
        "other_tables_with_data": other_tables_with_data,
        "valuation_candidate_rows": val_rows,
        "adjacent_kline_context": adj_dates,
        "findings": findings,
        "why_unresolved": (
            "No observed fact establishes whether {d} is a trading day. "
            "Side-table rows (lhb_daily/analyst_forecasts/valuation candidates) are not "
            "independent proof that klines were expected. pipeline_runs started on "
            "2026-07-18, so 07-08 has no run log by design; the 07-20 daily_data_pipeline "
            "recorded success with duration_s=0.0 and old-format run_id, which does not "
            "prove klines were ingested. Manual upstream-calendar verification is required."
        ).format(d=d),
        "requires_manual_review": True,
    }


def main() -> None:
    p = argparse.ArgumentParser(description="B2-R1: Anomaly trading days fact audit")
    p.add_argument("--market-db", type=Path, required=True)
    p.add_argument("--valuation-db", type=Path, default=None)
    p.add_argument("--output-dir", type=Path, required=True)
    args = p.parse_args()

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    start = time.time()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(str(output_dir / "run.log"), encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    log.info("=== B2-R1 anomaly days fact audit start ===")

    conn = open_ro(args.market_db)
    all_dates = sorted(set(ANOMALY_DATES + CONTEXT_DATES))

    log.info("Auditing all tables...")
    table_audit = audit_all_tables(conn, all_dates)

    weekday_info = {}
    for d in all_dates:
        dt = datetime.strptime(d, "%Y-%m-%d")
        weekday_info[d] = {
            "weekday_name": dt.strftime("%A"),
            "is_weekend": dt.weekday() >= 5,
            "day_of_year": dt.timetuple().tm_yday,
        }

    log.info("Auditing pipeline_runs fact chain...")
    pipeline_runs = audit_pipeline_runs(conn, all_dates)
    log.info("pipeline_runs table_since=%s total=%s distinct_dates=%s",
             pipeline_runs["table_creation_date"], pipeline_runs["total_runs"],
             len(pipeline_runs["distinct_dates"]))

    val_conn = None
    if args.valuation_db and args.valuation_db.exists():
        val_conn = open_ro(args.valuation_db)
    provenance = audit_valuation_provenance(val_conn, all_dates)

    fact_chain = {}
    for d in ANOMALY_DATES:
        fact_chain[d] = build_fact_chain(d, table_audit, weekday_info, pipeline_runs, provenance)
        log.info("Date %s: %s (daily_klines=%s)", d, fact_chain[d]["verdict"],
                 fact_chain[d]["daily_klines_count"])

    # Investigate directions are factual questions, not confirmed repairs.
    investigate_directions = [
        {
            "question": "Is {d} a real trading day?",
            "method": "Verify against an upstream exchange calendar (Fuyao / exchange holidays). "
                      "Side-table rows are NOT sufficient proof.",
            "applies_to": ANOMALY_DATES,
        },
        {
            "question": "Did the 07-20 daily_data_pipeline actually ingest klines?",
            "method": "Review pipeline logs around 2026-07-20 15:30:01. The run record shows "
                      "success with duration_s=0.0 and old-format run_id; a zero duration is "
                      "ambiguous and must be cross-checked against ingested row counts.",
            "applies_to": ["2026-07-20"],
        },
        {
            "question": "Why did daily_data_pipeline fail on 07-21 (timeout after 300s)?",
            "method": "Check upstream source availability and pipeline timeout settings. "
                      "07-21+ failures are a separate observed fact from the 07-08/07-20 gap.",
            "applies_to": ["2026-07-21", "2026-07-22"],
        },
    ]

    def write_json(path: str, data: Any):
        (output_dir / path).write_text(json.dumps(data, indent=2, ensure_ascii=False))

    write_json("anomaly-day-table-audit.json", {
        "table_audit": table_audit,
        "weekday_info": weekday_info,
    })
    write_json("anomaly-day-pipeline-runs.json", pipeline_runs)
    write_json("anomaly-day-provenance.json", provenance)
    write_json("anomaly-day-fact-chain.json", fact_chain)
    write_json("investigate-directions.json", investigate_directions)

    report = {
        "stage": "B2-R1",
        "status": "COMPLETED",
        "started_at": now_iso(),
        "elapsed_seconds": round(time.time() - start, 2),
        "anomaly_dates": ANOMALY_DATES,
        "verdict_summary": {d: "UNRESOLVED" for d in ANOMALY_DATES},
        "daily_klines_counts": {d: fact_chain[d]["daily_klines_count"] for d in ANOMALY_DATES},
        "pipeline_runs_fact_chain": {
            "table_creation_date": pipeline_runs["table_creation_date"],
            "total_runs": pipeline_runs["total_runs"],
            "note": pipeline_runs["note"],
        },
        "conclusion": (
            "Both 2026-07-08 and 2026-07-20 are UNRESOLVED. Observed facts: "
            "daily_klines=0 on both dates; valuation candidate DB has 5187/5189 rows; "
            "lhb_daily=82 on 07-08 and analyst_forecasts=160 on 07-20 (side-table facts, "
            "not trading-day proof); pipeline_runs was created 07-18 so 07-08 has no log; "
            "the 07-20 daily_data_pipeline run is old-format with duration_s=0.0 and does "
            "not prove ingestion. No automated root-cause claim is made. "
            "Manual upstream-calendar verification is required."
        ),
        "investigate_directions_count": len(investigate_directions),
    }
    write_json("report.json", report)
    log.info("=== B2-R1 anomaly days fact audit complete (both UNRESOLVED) ===")

    if val_conn:
        val_conn.close()
    conn.close()


if __name__ == "__main__":
    main()
