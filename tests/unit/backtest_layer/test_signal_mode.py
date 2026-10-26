"""Signal Mode 单元测试"""
import pytest
from backtest_layer.core.broker import Broker
from backtest_layer.core.models import CostConfig
from backtest_layer.modes.signal import SignalMode


def test_signal_mode_buy():
    broker = Broker(CostConfig(initial_capital=100000))
    mode = SignalMode(broker)
    signals = [{"code": "000001", "signal": "buy", "date": "2026-07-17"}]
    prices = {"000001": [15.0]}
    result = mode.run(signals, prices)
    assert result["trade_count"] == 1
    assert "000001" in broker.positions


def test_signal_mode_sell():
    broker = Broker(CostConfig(initial_capital=100000))
    broker.buy("000001", 15.0, 1000, "2026-07-16")
    mode = SignalMode(broker)
    signals = [{"code": "000001", "signal": "sell", "date": "2026-07-17"}]
    prices = {"000001": [16.0]}
    result = mode.run(signals, prices)
    assert "000001" not in broker.positions
