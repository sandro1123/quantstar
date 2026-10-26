"""PlanManager 单元测试"""
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


def test_cancel_exit(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "exit_pending")
    exec_layer.plan_manager.cancel_exit("000001")
    pool = exec_layer.stock_pool.get("000001")
    assert pool["status"] == "active"


def test_cancel_exit_noop(exec_layer):
    exec_layer.plan_manager.cancel_exit("999999")
    pool = exec_layer.stock_pool.get("999999")
    assert pool is None


def test_adjust_take_profit(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.plan_manager.adjust_take_profit("000001", 20.0, "调整止盈")
    pool = exec_layer.stock_pool.get("000001")
    assert pool["take_profit"] == 20.0


def test_adjust_take_profit_noop(exec_layer):
    exec_layer.plan_manager.adjust_take_profit("999999", 20.0, "noop")
    pool = exec_layer.stock_pool.get("999999")
    assert pool is None


def test_adjust_trailing_stop(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.plan_manager.adjust_trailing_stop("000001", 5.0, "调整移动止损")
    pool = exec_layer.stock_pool.get("000001")
    assert pool["trailing_stop"] == 5.0


def test_adjust_trailing_stop_noop(exec_layer):
    exec_layer.plan_manager.adjust_trailing_stop("999999", 5.0, "noop")
    pool = exec_layer.stock_pool.get("999999")
    assert pool is None


def test_adjust_entry_range(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.plan_manager.adjust_entry_range("000001", 10.0, 12.0, "调整入场区间")
    pool = exec_layer.stock_pool.get("000001")
    assert pool["entry_low"] == 10.0
    assert pool["entry_high"] == 12.0


def test_adjust_entry_range_noop(exec_layer):
    exec_layer.plan_manager.adjust_entry_range("999999", 10.0, 12.0, "noop")
    pool = exec_layer.stock_pool.get("999999")
    assert pool is None