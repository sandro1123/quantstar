"""正式退出结算：流水失败回滚，旧快照不能重复结算。"""
import pytest

from common.database import Database
from execution_layer.core.stock_pool import StockPool
from execution_layer.core.paper_broker import PaperBroker


@pytest.mark.parametrize("reject_record", [True, False])
def test_exit_and_record_atomic_with_stale_snapshot(monkeypatch, reject_record):
    db = Database(":memory:")
    pool = StockPool(db)
    pool._ensure_table()
    pool.action_record._ensure_table()
    conn = db.get_connection()
    conn.execute("INSERT INTO stock_pool (code,name,status,added_date,entry_price,position_qty) "
                 "VALUES ('000001','测试','exit_pending','2026-09-17',20,100)")
    if reject_record:
        conn.execute("""CREATE TRIGGER reject_exit_record BEFORE INSERT ON action_records
            WHEN NEW.action_type='exit'
            BEGIN SELECT RAISE(ABORT,'injected record failure'); END""")
    conn.commit()
    broker = PaperBroker(db, pool)
    monkeypatch.setattr(broker, "_send_notification", lambda *args: None)
    snapshot = pool.get("000001")
    try:
        result = broker.exit_with_fees("000001", 21, "test", snapshot)
        assert result.success is (not reject_record)
        row = conn.execute("SELECT status,exit_price FROM stock_pool WHERE code='000001'").fetchone()
        if reject_record:
            assert row["status"] == "exit_pending"
            assert row["exit_price"] is None
            assert conn.execute("SELECT COUNT(*) FROM action_records").fetchone()[0] == 0
        else:
            assert row["status"] == "exited"
            second = broker.exit_with_fees("000001", 22, "repeat", snapshot)
            assert second.success is False
            assert conn.execute("SELECT exit_price FROM stock_pool").fetchone()[0] == 21
            assert conn.execute("SELECT COUNT(*) FROM action_records WHERE action_type='exit'").fetchone()[0] == 1
    finally:
        db.close()
