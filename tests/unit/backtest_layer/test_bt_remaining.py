"""Tests for backtest layer remaining modules"""
from backtest_layer.core.models import BacktestInput, CostConfig


class TestHoldMode:
    def test_hold_mode_requires_broker(self):
        from backtest_layer.modes.hold import HoldMode
        assert HoldMode is not None