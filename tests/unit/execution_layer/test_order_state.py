"""G-E8: Order state machine tests"""
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


def test_order_state_import():
    from execution_layer.stats.display import OrderState
    os = OrderState(code="000001", order_type="entry", state="pending")
    assert os.code == "000001"
    assert os.order_type == "entry"
    assert os.state == "pending"
    assert os.requested_qty == 0
    assert os.filled_qty == 0
    assert os.requested_price == 0.0
    assert os.filled_price == 0.0
    assert os.created_at == ""
    assert os.filled_at == ""
    assert os.reason == ""
    assert os.state_history == []


def test_order_state_defaults():
    from execution_layer.stats.display import OrderState
    os = OrderState()
    assert os.code == ""
    assert os.order_type == ""
    assert os.state == "pending"
    assert os.requested_qty == 0
    assert os.filled_qty == 0
    assert os.requested_price == 0.0
    assert os.filled_price == 0.0
    assert os.created_at == ""
    assert os.filled_at == ""
    assert os.reason == ""
    assert os.state_history == []


def test_get_order_state_nonexistent(exec_layer):
    result = exec_layer.get_order_state("NONEXIST")
    assert result is None


def test_get_order_state_watch(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    result = exec_layer.get_order_state("000001")
    assert result is not None
    assert result.code == "000001"
    assert result.state == "pending"


def test_get_order_state_active(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "active")
    exec_layer.stock_pool.update("000001", entry_price=10.0, position_qty=100)
    result = exec_layer.get_order_state("000001")
    assert result is not None
    assert result.code == "000001"
    assert result.state == "executed"
    assert result.requested_qty == 100
    assert result.filled_qty == 100
    assert result.requested_price == 10.0
    assert result.filled_price == 10.0


def test_get_order_state_exited(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "exited")
    result = exec_layer.get_order_state("000001")
    assert result is not None
    assert result.state == "executed"


def test_get_order_state_with_state_history(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.action_record.write("000001", "add_to_watch", "添加到观察池", "manual")
    exec_layer.action_record.write("000001", "status_change", "状态变更 → pending_entry", "manual")
    result = exec_layer.get_order_state("000001")
    assert result is not None
    assert len(result.state_history) >= 2
    assert "add_to_watch" in result.state_history
    assert "status_change" in result.state_history


def test_get_order_state_machine_nonexistent(exec_layer):
    sm = exec_layer.stock_pool.get_order_state_machine("NONEXIST")
    assert sm.current == "nonexistent"
    assert sm.transitions == ["add_to_watch"]


def test_get_order_state_machine_watch(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    sm = exec_layer.stock_pool.get_order_state_machine("000001")
    assert sm.current == "watch"
    assert "pending_entry" in sm.transitions
    assert "exited" in sm.transitions


def test_get_order_state_machine_pending_entry(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "pending_entry")
    sm = exec_layer.stock_pool.get_order_state_machine("000001")
    assert sm.current == "pending_entry"
    assert "active" in sm.transitions
    assert "watch" in sm.transitions


def test_get_order_state_machine_active(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "active")
    sm = exec_layer.stock_pool.get_order_state_machine("000001")
    assert sm.current == "active"
    assert "exit_pending" in sm.transitions
    assert "exited" in sm.transitions


def test_get_order_state_machine_exit_pending(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "exit_pending")
    sm = exec_layer.stock_pool.get_order_state_machine("000001")
    assert sm.current == "exit_pending"
    assert "active" in sm.transitions
    assert "exited" in sm.transitions


def test_get_order_state_machine_exited(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "exited")
    sm = exec_layer.stock_pool.get_order_state_machine("000001")
    assert sm.current == "exited"
    assert sm.transitions == []