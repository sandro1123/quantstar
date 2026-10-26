"""S1 成本口径统一 — 三端（回测/执行/模拟盘）费率一致性回归测试"""
import pytest

from backtest_layer.core.broker import Broker
from backtest_layer.core.models import CostConfig
from execution_layer.core.constraint_checker import calculate_fees

PRICE = 10.0
QTY = 1000
AMOUNT = PRICE * QTY


class TestExecutionFees:
    def test_stamp_tax_only_sell_at_0005(self):
        buy = calculate_fees(PRICE, QTY, is_buy=True)
        sell = calculate_fees(PRICE, QTY, is_buy=False)
        assert buy["stamp_tax"] == 0.0
        assert sell["stamp_tax"] == pytest.approx(AMOUNT * 0.0005, abs=0.01)

    def test_transfer_fee_both_directions_at_00002(self):
        buy = calculate_fees(PRICE, QTY, is_buy=True)
        sell = calculate_fees(PRICE, QTY, is_buy=False)
        assert buy["transfer_fee"] == pytest.approx(AMOUNT * 0.00002, abs=0.01)
        assert sell["transfer_fee"] == pytest.approx(AMOUNT * 0.00002, abs=0.01)

    def test_sell_total_fee_breakdown(self):
        sell = calculate_fees(PRICE, QTY, is_buy=False)
        expected_commission = max(AMOUNT * 0.00025, 5.0)
        expected_stamp = AMOUNT * 0.0005
        expected_transfer = AMOUNT * 0.00002
        assert sell["commission"] == pytest.approx(expected_commission, abs=0.01)
        assert sell["total"] == pytest.approx(expected_commission + expected_stamp + expected_transfer, abs=0.01)
        assert sell["net_amount"] == pytest.approx(AMOUNT - sell["total"], abs=0.01)


class TestBacktestBrokerFees:
    def _broker(self):
        return Broker(CostConfig(initial_capital=1_000_000.0, slippage_rate=0.0))

    def test_buy_cost_includes_transfer_fee(self):
        broker = self._broker()
        result = broker.buy("600000", PRICE, QTY, "2026-08-14", order_type="limit")
        assert result.success
        assert result.commission == pytest.approx(max(AMOUNT * 0.00025, 5.0), abs=0.01)
        assert result.transfer_fee == pytest.approx(AMOUNT * 0.00002, abs=0.01)
        expected_cash = 1_000_000.0 - AMOUNT - result.commission - result.transfer_fee
        assert broker.cash == pytest.approx(expected_cash, abs=0.01)

    def test_sell_proceeds_deduct_commission_stamp_transfer(self):
        broker = self._broker()
        broker.buy("600000", PRICE, QTY, "2026-08-14", order_type="limit")
        result = broker.sell("600000", PRICE, QTY, "2026-08-15", order_type="limit")
        assert result.success
        assert result.commission == pytest.approx(max(AMOUNT * 0.00025, 5.0), abs=0.01)
        assert result.stamp_tax == pytest.approx(AMOUNT * 0.0005, abs=0.01)
        assert result.transfer_fee == pytest.approx(AMOUNT * 0.00002, abs=0.01)
        expected_cash = 1_000_000.0 - AMOUNT - max(AMOUNT * 0.00025, 5.0) - AMOUNT * 0.00002 \
            + AMOUNT - max(AMOUNT * 0.00025, 5.0) - AMOUNT * 0.0005 - AMOUNT * 0.00002
        assert broker.cash == pytest.approx(expected_cash, abs=0.01)


class TestThreeWayAlignment:
    def test_round_trip_fee_total_matches(self):
        broker = Broker(CostConfig(initial_capital=1_000_000.0, slippage_rate=0.0))
        buy_r = broker.buy("600000", PRICE, QTY, "2026-08-14", order_type="limit")
        sell_r = broker.sell("600000", PRICE, QTY, "2026-08-15", order_type="limit")
        exec_buy = calculate_fees(PRICE, QTY, is_buy=True)
        exec_sell = calculate_fees(PRICE, QTY, is_buy=False)

        broker_buy_total = buy_r.commission + buy_r.stamp_tax + buy_r.transfer_fee
        broker_sell_total = sell_r.commission + sell_r.stamp_tax + sell_r.transfer_fee
        assert broker_buy_total == pytest.approx(exec_buy["total"], abs=0.02)
        assert broker_sell_total == pytest.approx(exec_sell["total"], abs=0.02)
        assert broker_buy_total + broker_sell_total == pytest.approx(
            exec_buy["total"] + exec_sell["total"], abs=0.03)

    def test_fee_components_match_item_by_item(self):
        broker = Broker(CostConfig(initial_capital=1_000_000.0, slippage_rate=0.0))
        buy_r = broker.buy("600000", PRICE, QTY, "2026-08-14", order_type="limit")
        sell_r = broker.sell("600000", PRICE, QTY, "2026-08-15", order_type="limit")
        exec_buy = calculate_fees(PRICE, QTY, is_buy=True)
        exec_sell = calculate_fees(PRICE, QTY, is_buy=False)

        assert buy_r.commission == pytest.approx(exec_buy["commission"], abs=0.01)
        assert buy_r.transfer_fee == pytest.approx(exec_buy["transfer_fee"], abs=0.01)
        assert sell_r.commission == pytest.approx(exec_sell["commission"], abs=0.01)
        assert sell_r.stamp_tax == pytest.approx(exec_sell["stamp_tax"], abs=0.01)
        assert sell_r.transfer_fee == pytest.approx(exec_sell["transfer_fee"], abs=0.01)
