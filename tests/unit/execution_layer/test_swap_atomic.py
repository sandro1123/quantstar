"""换股退出状态和流水必须一同提交或回滚。"""
import sqlite3

import pytest

from common.database import Database
from execution_layer.core.stock_pool import StockPool


@pytest.mark.parametrize("reject_record", [True, False])
def test_swap_state_and_record_commit_together(reject_record):
    db = Database(":memory:")
    pool = StockPool(db)
    pool._ensure_table()
    pool.action_record._ensure_table()
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO stock_pool (code, name, status, added_date, entry_price, position_qty) "
        "VALUES ('000001', '测试股', 'active', '2026-09-17', 20, 100)")
    if reject_record:
        conn.execute("""CREATE TRIGGER reject_swap_record BEFORE INSERT ON action_records
            WHEN NEW.action_type = 'swap_exit'
            BEGIN SELECT RAISE(ABORT, 'injected record failure'); END""")
    conn.commit()
    try:
        if reject_record:
            with pytest.raises(sqlite3.IntegrityError, match="injected record failure"):
                pool.swap("000001", "000002", "调仓测试")
        else:
            assert pool.swap("000001", "000002", "调仓测试") is True
        row = conn.execute("SELECT status, exit_date, swap_count FROM stock_pool WHERE code='000001'").fetchone()
        records = conn.execute("SELECT action_type FROM action_records WHERE code='000001'").fetchall()
        if reject_record:
            assert row["status"] == "active"
            assert row["exit_date"] is None
            assert (row["swap_count"] or 0) == 0
            assert records == []
        else:
            assert row["status"] == "exited"
            assert row["exit_date"] is not None
            assert row["swap_count"] == 1
            assert [r[0] for r in records] == ["swap_exit"]
    finally:
        db.close()
