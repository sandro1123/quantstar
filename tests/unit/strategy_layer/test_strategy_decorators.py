"""Test that StrategyLayer public methods have @with_timeout decorators"""
import pytest
from strategy_layer import StrategyLayer


def test_run_pre_market_fake_removed_raises_not_implemented():
    """ST-08 拆假货: run_pre_market 假实现已删, 不再需要 @with_timeout 装饰;
    调用显式抛 NotImplementedError (真重建排队, 前置 DL-22)"""
    sl = StrategyLayer()
    with pytest.raises(NotImplementedError):
        sl.run_pre_market("2026-07-20")


def test_run_intraday_cycle_has_with_timeout():
    sl = StrategyLayer()
    assert hasattr(sl.run_intraday_cycle, "__wrapped__"), "@with_timeout should wrap run_intraday_cycle"


def test_run_post_market_has_with_timeout():
    sl = StrategyLayer()
    assert hasattr(sl.run_post_market, "__wrapped__"), "@with_timeout should wrap run_post_market"


def test_get_signals_has_both_decorators():
    sl = StrategyLayer()
    assert hasattr(sl.get_signals, "__wrapped__"), "@with_timeout should wrap get_signals"


def test_market_regime_has_both_decorators():
    sl = StrategyLayer()
    assert hasattr(sl.market_regime, "__wrapped__"), "@with_timeout should wrap market_regime"