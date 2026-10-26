"""Broker 单元测试"""
import pytest
from backtest_layer.core.broker import Broker
from backtest_layer.core.models import CostConfig


def test_buy_success():
    b = Broker(CostConfig(initial_capital=100000))
    result = b.buy("000001", 15.0, 100, "2026-07-17")
    assert result.success
    assert b.cash < 100000


def test_buy_insufficient_funds():
    b = Broker(CostConfig(initial_capital=100))
    result = b.buy("000001", 15.0, 100, "2026-07-17")
    assert not result.success


def test_sell_success():
    b = Broker(CostConfig(initial_capital=100000))
    b.buy("000001", 15.0, 100, "2026-07-17")
    result = b.sell("000001", 16.0, 100, "2026-07-22")
    assert result.success
    assert result.pnl > 0


def test_nav():
    b = Broker(CostConfig(initial_capital=100000))
    b.buy("000001", 15.0, 100, "2026-07-17")
    nav = b.nav({"000001": 16.0})
    assert nav > 100000
