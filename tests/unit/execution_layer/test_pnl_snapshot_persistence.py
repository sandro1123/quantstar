"""D5: 盯市快照落库 — 写库/读库/降级三态"""
import pytest

from common.database import Database
from execution_layer import ExecutionLayer
from execution_layer.tables import init_execution_tables


@pytest.fixture()
def el():
    db = Database(":memory:")
    db.init_db()
    el = ExecutionLayer(db=db)
    el.start()
    yield el
    el.shutdown()
    db.close()


def test_snapshot_persisted_to_db(el):
    """快照写入 intraday_pnl_snapshots 表"""
    active = [{"code": "000001", "unrealized_pnl_pct": 2.5},
              {"code": "000002", "unrealized_pnl_pct": -1.0}]
    el.intraday_monitor._record_pnl_snapshot(active)
    rows = el.db.get_connection().execute(
        "SELECT unrealized_pnl_pct, active_count FROM intraday_pnl_snapshots"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["unrealized_pnl_pct"] == 1.5
    assert rows[0]["active_count"] == 2


def test_trend_reads_from_db_across_processes(el):
    """get_intraday_pnl_trend 读库: 模拟跨进程 — 内存清空后仍能从库读回"""
    active = [{"code": "000001", "unrealized_pnl_pct": 3.0}]
    el.intraday_monitor._record_pnl_snapshot(active)
    el.intraday_monitor._pnl_snapshots.clear()  # 模拟新进程内存为空
    trend = el.intraday_monitor.get_intraday_pnl_trend()
    assert len(trend) == 1
    assert trend[0]["unrealized_pnl_pct"] == 3.0


def test_missing_table_degrades_to_memory(el):
    """表未建 (GO 前) → 写失败只 warning, 内存态仍可用"""
    el.db.get_connection().execute("DROP TABLE intraday_pnl_snapshots")
    active = [{"code": "000001", "unrealized_pnl_pct": 1.0}]
    el.intraday_monitor._record_pnl_snapshot(active)  # 不抛异常
    trend = el.intraday_monitor.get_intraday_pnl_trend()
    assert len(trend) == 1, "库不可用应回退内存态"
    assert trend[0]["unrealized_pnl_pct"] == 1.0


def test_ddl_registered_in_all_tables():
    """接线守卫: DDL 已注册进 init_execution_tables"""
    import sqlite3
    conn = sqlite3.connect(":memory:")
    init_execution_tables(conn)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    assert "intraday_pnl_snapshots" in tables
