"""RebalanceEngine 集成测试 — 验证 ExecutionLayer 实例化"""
import pytest
from common.database import Database
from execution_layer import ExecutionLayer
from execution_layer.core.rebalance_engine import RebalanceEngine


@pytest.fixture
def exec_layer():
    db = Database(":memory:")
    db.init_db()
    el = ExecutionLayer(db=db)
    el.start()
    yield el
    el.shutdown()
    db.close()


def test_rebalance_engine_instantiated(exec_layer):
    assert exec_layer.rebalance_engine is not None
    assert isinstance(exec_layer.rebalance_engine, RebalanceEngine)


def test_rebalance_check_add_not_in_pool(exec_layer):
    """check_add returns AddCheckResult when stock not in pool"""
    result = exec_layer.rebalance_engine.check_add("000001", 15.0, 100000.0)
    assert hasattr(result, 'can_add')
    assert result.can_add is True
    assert hasattr(result, 'max_add_qty')
