"""An already-consumed pending snapshot must not emit a second exit event."""
from unittest.mock import Mock

import pytest

from common.database import Database
from execution_layer import ExecutionLayer
from execution_layer.core.action_record import ActionRecord
from execution_layer.core.stock_pool import StockPool


@pytest.mark.parametrize("track_source", [False, True])
def test_stale_pending_snapshot_does_not_duplicate_exit(monkeypatch, track_source):
    monkeypatch.setattr("execution_layer.bj_date", lambda: "2026-09-17")
    monkeypatch.setattr("config_layer.cfg_get", lambda *args: track_source)
    db = Database(":memory:")
    pool = StockPool(db)
    pool._ensure_table()
    conn = db.get_connection()
    conn.execute("""INSERT INTO stock_pool
        (code, name, status, added_date, entry_date, entry_price, position_qty)
        VALUES ('688029', 'fixture', 'exit_pending', '2026-09-11',
                '2026-09-11', 72.29, 600)""")
    conn.execute("CREATE TABLE daily_klines (code TEXT, date TEXT, close REAL)")
    # Synthetic price in an in-memory fixture, not a production price claim.
    conn.execute("INSERT INTO daily_klines VALUES ('688029', '2026-09-17', 80.0)")
    conn.commit()
    layer = ExecutionLayer.__new__(ExecutionLayer)
    layer.db = db
    layer.stock_pool = pool
    layer.action_record = ActionRecord(db)
    snapshot = pool.get_by_status("exit_pending")
    layer._settle_pending_exits()
    first = dict(pool.get("688029"))
    assert first["status"] == "exited"
    assert first["exit_price"] == 80.0
    # Model a second caller that captured its pending rows before the first committed.
    monkeypatch.setattr(pool, "get_by_status", Mock(return_value=snapshot))
    layer._settle_pending_exits()
    assert not conn.in_transaction, "no-op settlement must release its write transaction"
    count = conn.execute("SELECT COUNT(*) FROM action_records WHERE code=? AND action_type=?",
                         ("688029", "exit")).fetchone()[0]
    assert count == 1, f"stale pending snapshot produced {count} exit events"
    assert dict(pool.get("688029")) == first
    assert not conn.in_transaction, "no-op settlement must release its write transaction"
