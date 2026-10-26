"""Tests for PaperBroker"""
from unittest.mock import MagicMock
from common.database import Database
from execution_layer.core.paper_broker import PaperBroker


class TestPaperBroker:
    def test_calc_qty_zero_for_invalid_price(self):
        db = Database(":memory:")
        broker = PaperBroker(db, MagicMock())
        assert broker._calc_qty(0) == 0
        assert broker._calc_qty(-1) == 0

    def test_calc_qty_returns_multiple_of_100(self):
        db = Database(":memory:")
        pool = MagicMock()
        pool.get_available_cash.return_value = 100000.0
        broker = PaperBroker(db, pool)
        qty = broker._calc_qty(10.0)
        assert qty % 100 == 0
        assert qty > 0

    def test_execute_entry_returns_false_for_nonexistent(self):
        db = Database(":memory:")
        pool = MagicMock()
        pool.get.return_value = None
        broker = PaperBroker(db, pool)
        assert broker.execute_entry("000001", 10.0, "test") is False

    def test_execute_entry_returns_false_for_wrong_status(self):
        db = Database(":memory:")
        pool = MagicMock()
        pool.get.return_value = {"code": "000001", "status": "active"}
        broker = PaperBroker(db, pool)
        assert broker.execute_entry("000001", 10.0, "test") is False

    def test_execute_entry_returns_false_when_price_out_of_range(self):
        db = Database(":memory:")
        pool = MagicMock()
        pool.get.return_value = {"code": "000001", "status": "pending_entry",
                                 "entry_low": 10.0, "entry_high": 12.0}
        broker = PaperBroker(db, pool)
        assert broker.execute_entry("000001", 15.0, "test") is False

    def test_dry_run_entry_returns_true(self):
        db = Database(":memory:")
        pool = MagicMock()
        pool.get.return_value = {"code": "000001", "status": "pending_entry",
                                 "entry_low": 10.0, "entry_high": 12.0,
                                 "name": "Test"}
        pool.get_available_cash.return_value = 100000.0
        broker = PaperBroker(db, pool, dry_run=True)
        assert broker.execute_entry("000001", 11.0, "test") is True

    def test_execute_exit_returns_false_for_nonexistent(self):
        db = Database(":memory:")
        pool = MagicMock()
        pool.get.return_value = None
        broker = PaperBroker(db, pool)
        assert broker.execute_exit("000001", 10.0, "test") is False

    def test_execute_exit_returns_false_for_wrong_status(self):
        db = Database(":memory:")
        pool = MagicMock()
        pool.get.return_value = {"code": "000001", "status": "active"}
        broker = PaperBroker(db, pool)
        assert broker.execute_exit("000001", 10.0, "test") is False

    def test_dry_run_exit_returns_true(self):
        db = Database(":memory:")
        pool = MagicMock()
        pool.get.return_value = {"code": "000001", "status": "exit_pending",
                                 "entry_price": 10.0, "position_qty": 1000,
                                 "holding_days": 5}
        broker = PaperBroker(db, pool, dry_run=True)
        assert broker.execute_exit("000001", 12.0, "take_profit") is True