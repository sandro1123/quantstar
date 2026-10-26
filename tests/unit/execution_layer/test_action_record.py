"""ActionRecord + fee calculation tests"""
import pytest
from common.database import Database
from execution_layer import ExecutionLayer
from execution_layer.core.constraint_checker import calculate_fees
from execution_layer.stats.display import CashFlowCard


def test_calculate_fees_buy():
    fees = calculate_fees(15.0, 100, is_buy=True)
    assert fees["commission"] >= 5.0
    assert fees["stamp_tax"] == 0
    assert fees["total"] > 0
    expected_net = 1500.0 - fees["total"]
    assert fees["net_amount"] == round(expected_net, 2)


def test_calculate_fees_sell():
    fees = calculate_fees(15.0, 100, is_buy=False)
    assert fees["stamp_tax"] > 0
    assert fees["total"] > fees["stamp_tax"]


def test_calculate_fees_large_buy():
    fees = calculate_fees(100.0, 1000, is_buy=True)
    assert fees["commission"] > 5.0
    assert fees["stamp_tax"] == 0
    assert fees["transfer_fee"] > 0


def test_calculate_fees_components():
    fees = calculate_fees(10.0, 1000, is_buy=False)
    assert "commission" in fees
    assert "stamp_tax" in fees
    assert "transfer_fee" in fees
    assert "total" in fees
    assert "net_amount" in fees
    assert fees["total"] == round(fees["commission"] + fees["stamp_tax"] + fees["transfer_fee"], 2)


def test_calculate_fees_net_amount_sell():
    fees = calculate_fees(20.0, 200, is_buy=False)
    expected_net = 4000.0 - fees["total"]
    assert fees["net_amount"] == round(expected_net, 2)


def test_cashflow_card_dataclass():
    card = CashFlowCard(code="000001", name="平安银行", action_type="buy",
                        price=15.0, quantity=100, gross_amount=1500.0,
                        fee=5.0, net_amount=1495.0, timestamp="2024-01-01 10:00:00")
    assert card.code == "000001"
    assert card.fee == 5.0
    assert card.net_amount == 1495.0


def test_cashflow_card_defaults():
    card = CashFlowCard()
    assert card.code == ""
    assert card.fee == 0.0
    assert card.net_amount == 0.0


@pytest.fixture
def exec_layer():
    db = Database(":memory:")
    db.init_db()
    el = ExecutionLayer(db=db)
    el.start()
    yield el
    el.shutdown()
    db.close()


def test_add_position_tracks_fees(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "active")
    exec_layer.stock_pool.update("000001", entry_price=15.0, position_qty=100)
    exec_layer.stock_pool.add_position("000001", 100, 15.0, "加仓")
    pool = exec_layer.stock_pool.get("000001")
    assert pool["position_qty"] == 200
    assert pool["total_fees"] > 0


def test_add_position_fees_in_action_record(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "active")
    exec_layer.stock_pool.update("000001", entry_price=15.0, position_qty=100)
    exec_layer.stock_pool.add_position("000001", 100, 15.0, "加仓")
    records = exec_layer.action_record.get_by_code("000001")
    add_records = [r for r in records if r["action_type"] == "add_position"]
    assert len(add_records) >= 1
    assert add_records[0]["new_value"] is not None
    assert add_records[0]["new_value"] > 0


def test_partial_exit_tracks_fees(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "active")
    exec_layer.stock_pool.update("000001", entry_price=15.0, position_qty=200)
    exec_layer.stock_pool.partial_exit("000001", 50, 15.0, "减仓")
    records = exec_layer.action_record.get_by_code("000001")
    exit_records = [r for r in records if r["action_type"] == "partial_exit"]
    assert len(exit_records) >= 1


def test_stock_pool_has_fee_columns(exec_layer):
    conn = exec_layer.db.get_connection()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(stock_pool)").fetchall()]
    assert "total_fees" in cols
    assert "net_return" in cols
    assert "net_return_pct" in cols