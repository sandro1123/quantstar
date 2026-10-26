"""Tests for strategy run provenance tracking"""
import json
from common.database import Database
from strategy_layer.provenance import ProvenanceTracker


def test_start_run_creates_record():
    db = Database(":memory:")
    db.init_db()
    tracker = ProvenanceTracker(db=db)

    run_id = tracker.start_run("2026-01-15", mode="live", parameters={"max_stocks": 50})
    assert run_id is not None
    assert "2026-01-15" in run_id

    record = tracker.get_run(run_id)
    assert record is not None
    assert record["date"] == "2026-01-15"
    assert record["mode"] == "live"
    assert record["status"] == "running"
    params = json.loads(record["parameters"])
    assert params["max_stocks"] == 50


def test_finish_run_updates_status():
    db = Database(":memory:")
    db.init_db()
    tracker = ProvenanceTracker(db=db)

    run_id = tracker.start_run("2026-01-15")
    tracker.finish_run(run_id, status="success", stock_count=100, signal_count=30, duration_s=12.5)

    record = tracker.get_run(run_id)
    assert record["status"] == "success"
    assert record["stock_count"] == 100
    assert record["signal_count"] == 30
    assert record["duration_s"] == 12.5


def test_list_runs_returns_records():
    db = Database(":memory:")
    db.init_db()
    tracker = ProvenanceTracker(db=db)

    id1 = tracker.start_run("2026-01-15", mode="live")
    id2 = tracker.start_run("2026-01-16", mode="backtest")

    runs = tracker.list_runs(limit=10)
    assert len(runs) == 2
    run_ids = [r["run_id"] for r in runs]
    assert id2 in run_ids
    assert id1 in run_ids