"""P0-7/G3: watchdog_reap — RC=124 强语义收尸行为边界测试。

- 收指定 key 当日 running 行 → failed + 审计 note, 永不 DELETE
- 二次守卫: 已收官的行不被覆盖
- 其他日期 / 其他管道 / 终态行不动
"""
import os
import sys

_QS_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(_QS_ROOT, "src"))
sys.path.insert(0, os.path.join(_QS_ROOT, "scripts"))

from common.database import Database
from ops_layer.core.tables import OPS_TABLES_DDL
from watchdog_reap import reap


def _db():
    d = Database(":memory:")
    d.init_db()
    conn = d.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.commit()
    return d


def _insert(db, run_id, name, date, status="running"):
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO pipeline_runs (run_id, pipeline_name, date, start_time, status)"
        " VALUES (?, ?, ?, '2026-08-25 15:30:00', ?)",
        (run_id, name, date, status),
    )
    conn.commit()


NOW = "2026-08-25 17:10:00"
TODAY = "2026-08-25"


def test_reaps_running_rows_with_audit_note_never_deletes():
    db = _db()
    _insert(db, "r1", "daily_data_pipeline", TODAY)
    before = db.get_connection().execute("SELECT COUNT(*) FROM pipeline_runs").fetchone()[0]
    reaped = reap(db.get_connection(), "daily_data_pipeline", TODAY, NOW)
    assert reaped == ["r1"]
    conn = db.get_connection()
    assert conn.execute("SELECT COUNT(*) FROM pipeline_runs").fetchone()[0] == before
    row = conn.execute(
        "SELECT status, error_msg FROM pipeline_runs WHERE run_id='r1'").fetchone()
    assert row["status"] == "failed" and "watchdog killed" in row["error_msg"]


def test_leaves_other_dates_pipes_and_terminal_rows_alone():
    db = _db()
    _insert(db, "other_day", "daily_data_pipeline", "2026-08-24")
    _insert(db, "other_pipe", "factor_update", TODAY)
    _insert(db, "done", "daily_data_pipeline", TODAY, status="success")
    _insert(db, "mine", "daily_data_pipeline", TODAY)
    reaped = reap(db.get_connection(), "daily_data_pipeline", TODAY, NOW)
    assert reaped == ["mine"]


def test_second_guard_does_not_overwrite_newer_outcome():
    db = _db()
    _insert(db, "g1", "daily_data_pipeline", TODAY)
    # 模拟收割瞬间他人先收官
    conn = db.get_connection()
    conn.execute("UPDATE pipeline_runs SET status='success', error_msg='fresh' "
                 "WHERE run_id='g1'")
    conn.commit()
    reaped = reap(conn, "daily_data_pipeline", TODAY, NOW)
    assert reaped == []
    row = conn.execute(
        "SELECT status, error_msg FROM pipeline_runs WHERE run_id='g1'").fetchone()
    assert row["status"] == "success" and row["error_msg"] == "fresh"


def test_no_rows_returns_empty_not_error():
    db = _db()
    assert reap(db.get_connection(), "ghost_pipe", TODAY, NOW) == []
