"""Tests for ops_layer/core/tables.py"""
import pytest
from common.database import Database
from ops_layer.core.tables import OPS_TABLES_DDL


@pytest.fixture
def db():
    database = Database(":memory:")
    yield database
    database.close()


def test_ddl_executes_cleanly(db):
    conn = db.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.commit()


def test_pipeline_runs_table_exists(db):
    conn = db.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.commit()
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='pipeline_runs'"
    ).fetchall()
    assert len(tables) == 1


def test_alerts_table_exists(db):
    conn = db.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.commit()
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='alerts'"
    ).fetchall()
    assert len(tables) == 1


def test_version_history_table_exists(db):
    conn = db.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.commit()
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='version_history'"
    ).fetchall()
    assert len(tables) == 1


def test_pipeline_runs_insert_and_query(db):
    conn = db.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.execute("""
        INSERT INTO pipeline_runs (run_id, pipeline_name, date, start_time, status)
        VALUES ('kline_20260718_100000', 'kline', '2026-07-18', '2026-07-18 10:00:00', 'running')
    """)
    conn.commit()
    row = conn.execute("SELECT * FROM pipeline_runs WHERE run_id='kline_20260718_100000'").fetchone()
    assert row is not None
    assert row["pipeline_name"] == "kline"
    assert row["status"] == "running"


def test_alerts_insert_and_query(db):
    conn = db.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.execute("""
        INSERT INTO alerts (severity, layer, title, detail, status, created_at)
        VALUES ('critical', 'data', 'db down', 'cannot connect', 'open', '2026-07-18 10:00:00')
    """)
    conn.commit()
    row = conn.execute("SELECT * FROM alerts WHERE title='db down'").fetchone()
    assert row is not None
    assert row["severity"] == "critical"
    assert row["status"] == "open"


def test_version_history_insert_and_query(db):
    conn = db.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.execute("""
        INSERT INTO version_history (code_version, code_branch, deployed_at)
        VALUES ('abc1234', 'main', '2026-07-18 10:00:00')
    """)
    conn.commit()
    row = conn.execute("SELECT * FROM version_history WHERE code_version='abc1234'").fetchone()
    assert row is not None
    assert row["code_branch"] == "main"


def test_ddl_idempotent(db):
    conn = db.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.executescript(OPS_TABLES_DDL)
    conn.commit()
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('pipeline_runs', 'alerts', 'version_history')"
    ).fetchall()
    assert len(tables) == 3
