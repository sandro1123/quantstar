import pytest
from common.database import Database
from execution_layer import ExecutionLayer
from execution_layer.stats.display import TradeFilter


@pytest.fixture
def exec_layer():
    db = Database(":memory:")
    db.init_db()
    el = ExecutionLayer(db=db)
    el.start()
    yield el
    el.shutdown()
    db.close()


def test_filter_trades_by_status(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.add("000002", "万科A")
    exec_layer.stock_pool.update_status("000001", "exited")
    results = exec_layer.filter_trades(status="exited")
    assert len(results) == 1
    assert results[0].code == "000001"


def test_filter_trades_by_return_range(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.add("000002", "万科A")
    exec_layer.stock_pool.update_status("000001", "exited")
    exec_layer.stock_pool.update_status("000002", "exited")
    exec_layer.stock_pool.update("000001", total_return_pct=5.0)
    exec_layer.stock_pool.update("000002", total_return_pct=-3.0)
    results = exec_layer.filter_trades(min_return=0)
    assert len(results) == 1
    assert results[0].code == "000001"


def test_filter_trades_by_date_range(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    results = exec_layer.filter_trades(date_from="2020-01-01", date_to="2029-12-31")
    assert len(results) == 1
    assert results[0].code == "000001"


def test_filter_trades_no_filters(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.add("000002", "万科A")
    results = exec_layer.filter_trades()
    assert len(results) == 2


def test_filter_trades_empty_result(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    results = exec_layer.filter_trades(status="exited")
    assert len(results) == 0


def test_filter_trades_empty_pool(exec_layer):
    results = exec_layer.filter_trades()
    assert len(results) == 0


def test_trade_filter_dataclass_defaults():
    tf = TradeFilter()
    assert tf.total == 0
    assert tf.by_status == {}
    assert tf.by_sector == {}
    assert tf.date_range == ("", "")


def test_trade_filter_dataclass_custom():
    tf = TradeFilter(
        total=10,
        by_status={"active": 5, "exited": 5},
        by_sector={"银行": 3, "地产": 7},
        date_range=("2024-01-01", "2024-12-31"),
    )
    assert tf.total == 10
    assert tf.by_status["active"] == 5
    assert tf.by_sector["银行"] == 3
    assert tf.date_range == ("2024-01-01", "2024-12-31")


def test_filter_trades_with_limit(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.add("000002", "万科A")
    exec_layer.stock_pool.add("000003", "深发展A")
    results = exec_layer.filter_trades(limit=2)
    assert len(results) == 2


def test_filter_trades_with_sector(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    result = exec_layer.filter_trades(sector="银行")
    assert isinstance(result, list)