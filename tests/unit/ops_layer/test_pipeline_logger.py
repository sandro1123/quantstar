"""Tests for ops_layer/pipeline_logger.py"""
import pytest
from common.database import Database
from ops_layer.core.tables import OPS_TABLES_DDL
from ops_layer.pipeline_logger import PipelineLogger


@pytest.fixture
def db():
    database = Database(":memory:")
    conn = database.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.commit()
    yield database
    database.close()


def test_start_run_returns_id(db):
    pl = PipelineLogger(db)
    run_id = pl.start_run("kline")
    assert "kline" in run_id
    assert len(run_id) > 10


def test_start_run_inserts_record(db):
    pl = PipelineLogger(db)
    run_id = pl.start_run("kline")
    conn = db.get_connection()
    row = conn.execute("SELECT * FROM pipeline_runs WHERE run_id=?", (run_id,)).fetchone()
    assert row is not None
    assert row["status"] == "running"
    assert row["pipeline_name"] == "kline"


def test_two_runs_in_same_second_have_unique_ids(db):
    pl = PipelineLogger(db)

    first = pl.start_run("kline")
    second = pl.start_run("kline")

    assert first != second
    count = db.get_connection().execute(
        "SELECT COUNT(*) FROM pipeline_runs WHERE pipeline_name='kline'"
    ).fetchone()[0]
    assert count == 2


def test_end_run_success(db):
    pl = PipelineLogger(db)
    run_id = pl.start_run("kline")
    pl.end_run(run_id, "success")
    conn = db.get_connection()
    row = conn.execute("SELECT * FROM pipeline_runs WHERE run_id=?", (run_id,)).fetchone()
    assert row["status"] == "success"
    assert row["end_time"] is not None


def test_end_run_failed_with_error(db):
    pl = PipelineLogger(db)
    run_id = pl.start_run("financial")
    pl.end_run(run_id, "failed", "timeout")
    conn = db.get_connection()
    row = conn.execute("SELECT * FROM pipeline_runs WHERE run_id=?", (run_id,)).fetchone()
    assert row["status"] == "failed"
    assert row["error_msg"] == "timeout"


def test_get_recent_runs(db):
    pl = PipelineLogger(db)
    run_id1 = pl.start_run("kline")
    pl.end_run(run_id1, "success")
    run_id2 = pl.start_run("financial")
    pl.end_run(run_id2, "failed", "error")
    runs = pl.get_recent_runs(days=7)
    assert len(runs) == 2
    assert runs[0].pipeline_name == "financial"
    assert runs[1].pipeline_name == "kline"
