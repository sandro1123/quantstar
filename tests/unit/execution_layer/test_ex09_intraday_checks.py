"""EX-09: intraday_checks 死表激活 — _scan 每轮对每只标的写一条检查记录

口径:
- 触发 → status='triggered', current_price=引擎评估价, exit 记 trigger_price=exit_low
- 未触发 → status='no_action', current_price 回退持仓 last_price (checker 未触发时返回 None, 不携带价格)
- 检查异常 → status='error' + detail, 扫描继续 (不中断整轮)
- 批量 INSERT (executemany) 每轮一次; 写表失败只 warning, 绝不影响扫描主流程
"""
from unittest.mock import MagicMock

from common.database import Database
from execution_layer.intraday.monitor import IntradayMonitor
from execution_layer.tables import INTRA_DAY_DDL


def _db_with_checks_table() -> Database:
    db = Database(":memory:")
    conn = db.get_connection()
    conn.execute(INTRA_DAY_DDL)
    conn.commit()
    return db


def _rows(db) -> list[tuple]:
    return db.get_connection().execute(
        "SELECT code, check_type, current_price, trigger_price, status, detail, "
        "check_time, check_round, latency_ms FROM intraday_checks ORDER BY id"
    ).fetchall()


def _monitor(db, active=None, pending=None) -> IntradayMonitor:
    pool = MagicMock()
    pool.get_by_status.side_effect = lambda s: {
        "active": active or [], "pending_entry": pending or [],
    }.get(s, [])
    return IntradayMonitor(db, pool)


class TestScanWritesCheckRows:
    def test_exit_triggered_and_entry_no_action_rows(self):
        db = _db_with_checks_table()
        active = [{"code": "000001", "last_price": 9.5}]
        pending = [{"code": "000002", "last_price": 12.0}]
        monitor = _monitor(db, active, pending)
        monitor.exit_checker = MagicMock()
        monitor.exit_checker.check.return_value = {
            "price": 9.0, "reason": "止损", "triggered": True,
            "action": "sell", "exit_low": 8.8,
        }
        monitor.entry_checker = MagicMock()
        monitor.entry_checker.check.return_value = None

        monitor._scan()

        rows = _rows(db)
        assert len(rows) == 2
        by_type = {r[1]: r for r in rows}
        exit_row = by_type["exit"]
        assert exit_row[0] == "000001"
        assert exit_row[2] == 9.0        # current_price = 引擎评估价
        assert exit_row[3] == 8.8        # trigger_price = exit_low
        assert exit_row[4] == "triggered"
        assert "止损" in exit_row[5]
        entry_row = by_type["entry"]
        assert entry_row[0] == "000002"
        assert entry_row[2] == 12.0      # 未触发 → 持仓 last_price
        assert entry_row[3] is None
        assert entry_row[4] == "no_action"
        for r in rows:
            assert r[6] and r[7]         # check_time / check_round 非空
            assert r[8] >= 0             # latency_ms
        # 触发 → mark_exit_trigger 原行为不变
        monitor.stock_pool.mark_exit_trigger.assert_called_once()

    def test_entry_triggered_row(self):
        db = _db_with_checks_table()
        monitor = _monitor(db, pending=[{"code": "000002", "last_price": 12.0}])
        monitor.entry_checker = MagicMock()
        monitor.entry_checker.check.return_value = {
            "price": 11.5, "reason": "触价", "triggered": True,
        }
        monitor.paper_broker = MagicMock()

        monitor._scan()

        rows = _rows(db)
        assert len(rows) == 1
        assert rows[0][1] == "entry"
        assert rows[0][2] == 11.5
        assert rows[0][4] == "triggered"
        assert "触价" in rows[0][5]
        # R2: _scan 改调 entry_with_fees (execute_entry 的底层, 行为不变),
        # 以取 PaperTradeResult 供触价卡佐证行
        monitor.paper_broker.entry_with_fees.assert_called_once()

    def test_check_error_records_error_row_and_continues(self):
        db = _db_with_checks_table()
        active = [{"code": "000001", "last_price": 9.5},
                  {"code": "000003", "last_price": 5.0}]
        monitor = _monitor(db, active)
        monitor.exit_checker = MagicMock()
        monitor.exit_checker.check.side_effect = [RuntimeError("boom"), None]

        monitor._scan()

        rows = _rows(db)
        assert len(rows) == 2
        assert rows[0][4] == "error"
        assert "boom" in rows[0][5]
        assert rows[1][4] == "no_action"  # 单只异常不中断整轮

    def test_no_checkers_no_rows(self):
        db = _db_with_checks_table()
        monitor = _monitor(db, [{"code": "000001"}])
        monitor._scan()
        assert _rows(db) == []

    def test_missing_table_does_not_break_scan(self):
        db = Database(":memory:")  # 无 intraday_checks 表
        monitor = _monitor(db, [{"code": "000001", "last_price": 9.5}])
        monitor.exit_checker = MagicMock()
        monitor.exit_checker.check.return_value = None

        monitor._scan()  # 不得抛异常

        assert len(monitor.get_intraday_pnl_trend()) == 1  # 扫描其余职责不受影响

    def test_two_scans_accumulate_rounds(self):
        db = _db_with_checks_table()
        monitor = _monitor(db, [{"code": "000001", "last_price": 9.5}])
        monitor.exit_checker = MagicMock()
        monitor.exit_checker.check.return_value = None

        monitor._scan()
        monitor._scan()

        assert len(_rows(db)) == 2  # 每轮每标的一条
