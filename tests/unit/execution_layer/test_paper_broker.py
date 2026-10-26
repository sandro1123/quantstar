"""Tests for PaperBroker"""
from unittest.mock import MagicMock
from common.database import Database
from execution_layer.core.paper_broker import PaperBroker
from execution_layer.tables import init_execution_tables


def _init_exec_db():
    db = Database(":memory:")
    init_execution_tables(db.get_connection())
    db.commit()
    return db


class TestPaperBroker:
    def test_calc_qty_zero_for_invalid_price(self):
        db = _init_exec_db()
        broker = PaperBroker(db, MagicMock())
        assert broker._calc_qty(0) == 0
        assert broker._calc_qty(-1) == 0

    def test_calc_qty_returns_multiple_of_100(self):
        db = _init_exec_db()
        pool = MagicMock()
        pool.get_available_cash.return_value = 100000.0
        broker = PaperBroker(db, pool)
        qty = broker._calc_qty(10.0)
        assert qty % 100 == 0
        assert qty > 0

    def test_execute_entry_returns_false_for_nonexistent(self):
        db = _init_exec_db()
        pool = MagicMock()
        pool.get.return_value = None
        broker = PaperBroker(db, pool)
        assert broker.execute_entry("000001", 10.0, "test") is False

    def test_execute_entry_returns_false_for_wrong_status(self):
        db = _init_exec_db()
        pool = MagicMock()
        pool.get.return_value = {"code": "000001", "status": "active"}
        broker = PaperBroker(db, pool)
        assert broker.execute_entry("000001", 10.0, "test") is False

    def test_execute_entry_returns_false_when_price_out_of_range(self):
        db = _init_exec_db()
        pool = MagicMock()
        pool.get.return_value = {"code": "000001", "status": "pending_entry",
                                 "entry_low": 10.0, "entry_high": 12.0}
        broker = PaperBroker(db, pool)
        assert broker.execute_entry("000001", 15.0, "test") is False

    def test_dry_run_entry_returns_true(self):
        db = _init_exec_db()
        pool = MagicMock()
        pool.get.return_value = {"code": "000001", "status": "pending_entry",
                                 "entry_low": 10.0, "entry_high": 12.0,
                                 "name": "Test"}
        pool.get_available_cash.return_value = 100000.0
        broker = PaperBroker(db, pool, dry_run=True)
        assert broker.execute_entry("000001", 11.0, "test") is True

    def test_execute_exit_returns_false_for_nonexistent(self):
        db = _init_exec_db()
        pool = MagicMock()
        pool.get.return_value = None
        broker = PaperBroker(db, pool)
        assert broker.execute_exit("000001", 10.0, "test") is False

    def test_execute_exit_returns_false_for_wrong_status(self):
        db = _init_exec_db()
        pool = MagicMock()
        pool.get.return_value = {"code": "000001", "status": "active"}
        broker = PaperBroker(db, pool)
        assert broker.execute_exit("000001", 10.0, "test") is False

    def test_dry_run_exit_returns_true(self):
        db = _init_exec_db()
        pool = MagicMock()
        pool.get.return_value = {"code": "000001", "status": "exit_pending",
                                 "entry_price": 10.0, "position_qty": 1000,
                                 "holding_days": 5}
        broker = PaperBroker(db, pool, dry_run=True)
        assert broker.execute_exit("000001", 12.0, "take_profit") is True


# -- Issue #8: paper trading 须穿过同款 ProtectionGate ------------------

def _entry_pool(code="000001", status="pending_entry", **overrides):
    pool = MagicMock()
    pool.get.return_value = {"code": code, "status": status,
                             "entry_low": 10.0, "entry_high": 12.0,
                             "name": "Test", **overrides}
    pool.get_available_cash.return_value = 100000.0
    return pool


def test_paper_entry_blocked_when_protection_gate_denies():
    """纸交易入场被风控闸门拦截时必须返回失败 (Issue #8)。"""
    db = _init_exec_db()
    pool = _entry_pool()
    broker = PaperBroker(db, pool)
    import execution_layer.core.paper_broker as pb_mod
    orig_check = pb_mod.ProtectionGate.check
    pb_mod.ProtectionGate.check = staticmethod(
        lambda code, current_time: (False, "回撤熔断: 模拟熔断"))
    try:
        result = broker.entry_with_fees("000001", 11.0, "test")
        assert not result.success
        assert "protection_gate" in result.reason
    finally:
        pb_mod.ProtectionGate.check = orig_check


def test_paper_entry_allows_when_protection_gate_passes():
    """纸交易入场在风控闸门放行时应正常执行 (Issue #8)。"""
    db = _init_exec_db()
    pool = _entry_pool()
    broker = PaperBroker(db, pool)
    import execution_layer.core.paper_broker as pb_mod
    orig_check = pb_mod.ProtectionGate.check
    pb_mod.ProtectionGate.check = staticmethod(
        lambda code, current_time: (True, ""))
    try:
        result = broker.entry_with_fees("000001", 11.0, "test")
        assert result.success
        assert result.reason == "test"
    finally:
        pb_mod.ProtectionGate.check = orig_check
