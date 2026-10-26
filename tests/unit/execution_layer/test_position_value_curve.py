"""Tests for G-E10: Position value curve during holding period"""
import pytest
from common.database import Database
from execution_layer import ExecutionLayer


@pytest.fixture
def exec_layer():
    db = Database(":memory:")
    db.init_db()
    el = ExecutionLayer(db=db)
    el.start()
    yield el
    el.shutdown()
    db.close()


def test_pnl_tracker_get_position_value_curve_returns_data(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "active")
    exec_layer.stock_pool.update("000001", entry_price=10.0, last_price=11.0,
                                  last_price_date="2024-01-15", unrealized_pnl_pct=10.0)
    curve = exec_layer.pnl_tracker.get_position_value_curve("000001")
    assert len(curve) == 1
    assert curve[0]["date"] == "2024-01-15"
    assert curve[0]["price"] == 11.0
    assert curve[0]["pnl_pct"] == 10.0


def test_pnl_tracker_get_position_value_curve_no_price_data(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "active")
    curve = exec_layer.pnl_tracker.get_position_value_curve("000001")
    assert curve == []


def test_execution_layer_get_position_value_curve_nonexistent(exec_layer):
    curve = exec_layer.get_position_value_curve("NONEXIST")
    assert curve == []


def test_execution_layer_get_position_value_curve_with_data(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "active")
    exec_layer.stock_pool.update("000001", entry_price=10.0, last_price=11.0,
                                  last_price_date="2024-01-15", unrealized_pnl_pct=10.0)
    curve = exec_layer.get_position_value_curve("000001")
    assert len(curve) == 1
    assert curve[0].date == "2024-01-15"
    assert curve[0].price == 11.0
    assert curve[0].pnl_pct == 10.0