"""S1 F1.5 — PaperBroker 含费成交回归测试 (dry_run, 不触 DB)"""
import pytest

from execution_layer.core.paper_broker import PaperBroker, PaperTradeResult

PRICE = 10.0


class _FakePool:
    def __init__(self, items):
        self.items = items

    def get(self, code):
        return self.items.get(code)

    def get_available_cash(self, initial):
        return initial


def _broker(items, dry_run=True):
    return PaperBroker(db=None, stock_pool=_FakePool(items), dry_run=dry_run)


class TestPaperEntryFees:
    def test_entry_dry_run_returns_fee_detail(self):
        broker = _broker({"600000": {"code": "600000", "name": "测试", "status": "pending_entry"}})
        broker._calc_qty = lambda price: 1000
        result = broker.entry_with_fees("600000", PRICE, "测试买入")
        assert isinstance(result, PaperTradeResult)
        assert result.success
        amount = 1000 * PRICE
        assert result.gross_amount == pytest.approx(amount, abs=0.01)
        assert result.commission == pytest.approx(max(amount * 0.00025, 5.0), abs=0.01)
        assert result.stamp_tax == 0.0
        assert result.transfer_fee == pytest.approx(amount * 0.00002, abs=0.01)
        assert result.total_fees == pytest.approx(result.commission + result.transfer_fee, abs=0.01)
        assert result.net_amount == pytest.approx(amount + result.total_fees, abs=0.01)

    def test_entry_execute_compat_returns_bool(self):
        broker = _broker({"600000": {"code": "600000", "name": "测试", "status": "pending_entry"}})
        broker._calc_qty = lambda price: 1000
        assert broker.execute_entry("600000", PRICE, "测试") is True

    def test_entry_insufficient_cash_fails_with_fees(self):
        items = {"600000": {"code": "600000", "name": "测试", "status": "pending_entry"}}
        broker = _broker(items)
        broker._initial_capital = 100.0
        broker._calc_qty = lambda price: 1000
        result = broker.entry_with_fees("600000", PRICE, "测试买入")
        assert not result.success
        assert result.reason == "insufficient_cash"
        assert result.total_fees > 0


class TestPaperExitFees:
    def test_exit_dry_run_returns_fee_detail_and_net_return(self):
        item = {"code": "600000", "name": "测试", "status": "exit_pending",
                "entry_price": 9.0, "position_qty": 1000, "holding_days": 5}
        broker = _broker({"600000": item})
        result = broker.exit_with_fees("600000", PRICE, "测试卖出")
        assert result.success
        gross = 1000 * PRICE
        assert result.gross_amount == pytest.approx(gross, abs=0.01)
        assert result.commission == pytest.approx(max(gross * 0.00025, 5.0), abs=0.01)
        assert result.stamp_tax == pytest.approx(gross * 0.0005, abs=0.01)
        assert result.transfer_fee == pytest.approx(gross * 0.00002, abs=0.01)
        assert result.net_amount == pytest.approx(gross - result.total_fees, abs=0.01)
        expected_return = (result.net_amount - 1000 * 9.0) / (1000 * 9.0) * 100
        assert result.return_pct == pytest.approx(expected_return, abs=0.01)

    def test_exit_execute_compat_returns_bool(self):
        item = {"code": "600000", "name": "测试", "status": "exit_pending",
                "entry_price": 9.0, "position_qty": 1000, "holding_days": 5}
        broker = _broker({"600000": item})
        assert broker.execute_exit("600000", PRICE, "测试") is True

    def test_exit_invalid_status_fails(self):
        broker = _broker({"600000": {"code": "600000", "status": "active"}})
        result = broker.exit_with_fees("600000", PRICE, "测试")
        assert not result.success
        assert result.reason == "invalid_status"
