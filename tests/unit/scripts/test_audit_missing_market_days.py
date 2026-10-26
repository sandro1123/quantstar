"""Tests for B2-R1 audit_missing_market_days.py."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent / "scripts"))
from audit_missing_market_days import (
    ANOMALY_DATES,
    CONTEXT_DATES,
    _is_old_format_run_id,
    audit_all_tables,
    audit_pipeline_runs,
    audit_table,
    build_fact_chain,
    now_iso,
)


@pytest.fixture
def memory_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE daily_klines (code TEXT, date TEXT, open REAL)")
    conn.execute("CREATE TABLE lhb_daily (code TEXT, date TEXT, name TEXT)")
    conn.execute("CREATE TABLE stock_moneyflow (code TEXT, date TEXT, net_inflow REAL)")
    conn.execute("CREATE TABLE pipeline_runs (id INTEGER, pipeline_name TEXT, status TEXT, started_at TEXT)")
    conn.execute("INSERT INTO daily_klines VALUES ('000001', '2026-07-07', 10.0)")
    conn.execute("INSERT INTO daily_klines VALUES ('000001', '2026-07-09', 10.5)")
    conn.execute("INSERT INTO lhb_daily VALUES ('000001', '2026-07-08', 'test')")
    conn.execute("INSERT INTO lhb_daily VALUES ('000002', '2026-07-08', 'test2')")
    conn.execute("INSERT INTO stock_moneyflow VALUES ('000001', '2026-07-20', 100.0)")
    conn.execute("INSERT INTO pipeline_runs VALUES (1, 'kline_ingest', 'failed', '2026-07-08')")
    conn.commit()
    return conn


@pytest.fixture
def pipeline_db():
    """pipeline_runs with the real production schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE pipeline_runs (run_id TEXT PRIMARY KEY, pipeline_name TEXT NOT NULL, "
        "date TEXT NOT NULL, start_time TEXT NOT NULL, end_time TEXT, status TEXT NOT NULL, "
        "error_msg TEXT, duration_s REAL, version TEXT, config_hash TEXT, created_at TEXT)"
    )
    conn.execute(
        "INSERT INTO pipeline_runs VALUES "
        "('daily_data_pipeline_2026-07-20_153001', 'daily_data_pipeline', '2026-07-20', "
        "'2026-07-20 15:30:01', '2026-07-20 15:30:01', 'success', '', 0.0, NULL, NULL, "
        "'2026-07-20 15:30:01')"
    )
    conn.execute(
        "INSERT INTO pipeline_runs VALUES "
        "('daily_data_pipeline_2026-07-21_153004-8f3a4c1e-7b2d-4e5f-9a0b-1c2d3e4f5a6b', "
        "'daily_data_pipeline', '2026-07-21', '2026-07-21 15:30:04', '2026-07-21 15:35:04', "
        "'failed', 'timeout after 300s', 300.0, NULL, NULL, '2026-07-21 15:30:04')"
    )
    conn.execute(
        "INSERT INTO pipeline_runs VALUES "
        "('health_check_2026-07-18_154153', 'health_check', '2026-07-18', "
        "'2026-07-18 15:41:53', '2026-07-18 15:41:53', 'success', '', NULL, NULL, NULL, "
        "'2026-07-18 15:41:53')"
    )
    conn.commit()
    return conn


def test_audit_table_normal(memory_db):
    result = audit_table(memory_db, "daily_klines", ["2026-07-07", "2026-07-08", "2026-07-09"])
    assert result["2026-07-07"] == 1
    assert result["2026-07-08"] == 0
    assert result["2026-07-09"] == 1


def test_audit_table_missing():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE t (date TEXT)")
    result = audit_table(conn, "t", ["2026-01-01"])
    assert result["2026-01-01"] == 0


def test_audit_all_tables(memory_db):
    result = audit_all_tables(memory_db, ["2026-07-08", "2026-07-20"])
    assert "daily_klines" in result
    assert "lhb_daily" in result
    assert result["daily_klines"]["2026-07-08"] == 0
    assert result["lhb_daily"]["2026-07-08"] == 2
    assert result["daily_klines"]["2026-07-20"] == 0
    assert result["stock_moneyflow"]["2026-07-20"] == 1


def test_anomaly_dates_defined():
    assert "2026-07-08" in ANOMALY_DATES
    assert "2026-07-20" in ANOMALY_DATES


def test_context_dates():
    assert "2026-07-07" in CONTEXT_DATES
    assert "2026-07-09" in CONTEXT_DATES
    assert "2026-07-17" in CONTEXT_DATES
    assert "2026-07-21" in CONTEXT_DATES


def test_now_iso():
    s = now_iso()
    assert "T" in s
    assert s.endswith("+00:00") or s.endswith("Z")


def test_is_old_format_run_id():
    """Old format (no uuid) vs new format (trailing uuid) detection."""
    assert _is_old_format_run_id("daily_data_pipeline_2026-07-20_153001") is True
    assert _is_old_format_run_id("health_check_2026-07-18_154153") is True
    assert _is_old_format_run_id(
        "daily_data_pipeline_2026-07-21_153004-8f3a4c1e-7b2d-4e5f-9a0b-1c2d3e4f5a6b"
    ) is False
    assert _is_old_format_run_id("") is False


def test_audit_pipeline_runs(pipeline_db):
    """pipeline_runs fact chain: table since 07-18, old-format 07-20 run."""
    result = audit_pipeline_runs(pipeline_db, ["2026-07-08", "2026-07-20", "2026-07-21"])
    assert result["table_exists"] is True
    assert result["table_creation_date"] == "2026-07-18"
    assert result["total_runs"] == 3
    # 07-08 has no log (table created later) - by design, not an error.
    assert result["per_date"]["2026-07-08"]["run_count"] == 0
    # 07-20: old-format run_id, success, duration_s=0.0, version/config_hash NULL.
    p20 = result["per_date"]["2026-07-20"]
    assert p20["old_format_run_ids"] == ["daily_data_pipeline_2026-07-20_153001"]
    ddp = p20["daily_data_pipeline"][0]
    assert ddp["status"] == "success"
    assert ddp["duration_s"] == 0.0
    assert ddp["version"] is None
    assert ddp["config_hash"] is None
    assert ddp["start_time"] == ddp["end_time"]
    # 07-21: new-format failed run.
    p21 = result["per_date"]["2026-07-21"]
    assert p21["daily_data_pipeline"][0]["status"] == "failed"
    assert p21["daily_data_pipeline"][0]["error_msg"] == "timeout after 300s"
    assert p21["old_format_run_ids"] == []


def test_build_fact_chain_unresolved(pipeline_db):
    """Both anomaly dates must be UNRESOLVED regardless of side-table data."""
    table_audit = audit_all_tables(pipeline_db, ["2026-07-08", "2026-07-20", "2026-07-21"])
    weekday_info = {}
    for d in ANOMALY_DATES:
        import datetime as _dt
        dt = _dt.datetime.strptime(d, "%Y-%m-%d")
        weekday_info[d] = {
            "weekday_name": dt.strftime("%A"),
            "is_weekend": dt.weekday() >= 5,
            "day_of_year": dt.timetuple().tm_yday,
        }
    pr = audit_pipeline_runs(pipeline_db, ANOMALY_DATES)
    provenance = {}  # no valuation DB in test
    for d in ANOMALY_DATES:
        fc = build_fact_chain(d, table_audit, weekday_info, pr, provenance)
        assert fc["verdict"] == "UNRESOLVED"
        assert fc["requires_manual_review"] is True
        assert "FORMAL_KLINE_INGESTION_GAP" not in fc["why_unresolved"]
        assert "daily_klines rows on" in " ".join(fc["findings"])


def test_build_fact_chain_records_side_tables(memory_db):
    """Side-table data is reported as a FACT, not as a root-cause label."""
    import datetime as _dt
    d = "2026-07-08"
    weekday_info = {d: {
        "weekday_name": _dt.datetime.strptime(d, "%Y-%m-%d").strftime("%A"),
        "is_weekend": False,
        "day_of_year": 0,
    }}
    table_audit = audit_all_tables(memory_db, [d, "2026-07-07", "2026-07-09"])
    pr = audit_pipeline_runs(memory_db, [d])
    fc = build_fact_chain(d, table_audit, weekday_info, pr, {})
    assert fc["verdict"] == "UNRESOLVED"
    assert fc["other_tables_with_data"] == ["lhb_daily=2"]
    joined = " ".join(fc["findings"])
    assert "does NOT establish that klines were expected" in joined
