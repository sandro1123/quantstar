"""策略感知 run mode (backtest/live/paper) 单元测试"""
import pytest
from unittest.mock import MagicMock, patch
from strategy_layer.engine import FactorEngine
from strategy_layer import StrategyLayer


class TestFactorEngineMode:
    def test_default_mode_is_live(self):
        engine = FactorEngine()
        assert engine.mode == "live"

    def test_custom_mode(self):
        engine = FactorEngine(mode="backtest")
        assert engine.mode == "backtest"

    def test_mode_property_roundtrip(self):
        for m in ("live", "backtest", "paper"):
            engine = FactorEngine(mode=m)
            assert engine.mode == m


class TestStrategyLayerMode:
    def test_default_mode_is_live(self):
        layer = StrategyLayer()
        assert layer.mode == "live"

    def test_custom_mode_constructor(self):
        layer = StrategyLayer(mode="backtest")
        assert layer.mode == "backtest"
        assert layer.factor_engine.mode == "backtest"

    def test_run_daily_mode_propagation(self):
        """run_daily passes mode down to FactorEngine"""
        layer = StrategyLayer(mode="live")
        with patch.object(layer.factor_engine, "compute_all", return_value=MagicMock()) as mock_compute:
            with patch.object(layer, "_get_stock_codes", return_value=["000001.SZ"]):
                with patch.object(layer, "market_regime", return_value="neutral"):
                    with patch.object(layer.aggregator, "aggregate", return_value=[]):
                        with patch.object(layer.selector, "run", return_value=MagicMock(candidates=[])):
                            with patch.object(layer.signal_generator, "generate", return_value=[]):
                                with patch.object(layer.deep_review, "review_candidates", return_value=[]):
                                    result = layer.run_daily("2024-01-02", mode="backtest")
        assert result is not None
        assert layer.factor_engine.mode == "backtest"

    def test_run_daily_default_mode(self):
        layer = StrategyLayer(mode="paper")
        with patch.object(layer.factor_engine, "compute_all", return_value=MagicMock()) as mock_compute:
            with patch.object(layer, "_get_stock_codes", return_value=["000001.SZ"]):
                with patch.object(layer, "market_regime", return_value="neutral"):
                    with patch.object(layer.aggregator, "aggregate", return_value=[]):
                        with patch.object(layer.selector, "run", return_value=MagicMock(candidates=[])):
                            with patch.object(layer.signal_generator, "generate", return_value=[]):
                                with patch.object(layer.deep_review, "review_candidates", return_value=[]):
                                    result = layer.run_daily("2024-01-02")
        assert result is not None
        assert layer.factor_engine.mode == "paper"

    def test_three_modes_distinct(self):
        """三个模式各自产生不同的 logging 行为（不崩溃）"""
        for mode in ("live", "backtest", "paper"):
            layer = StrategyLayer(mode=mode)
            with patch.object(layer.factor_engine, "compute_all", return_value=MagicMock()):
                with patch.object(layer, "_get_stock_codes", return_value=["000001.SZ"]):
                    with patch.object(layer, "market_regime", return_value="neutral"):
                        with patch.object(layer.aggregator, "aggregate", return_value=[]):
                            with patch.object(layer.selector, "run", return_value=MagicMock(candidates=[])):
                                with patch.object(layer.signal_generator, "generate", return_value=[]):
                                    with patch.object(layer.deep_review, "review_candidates", return_value=[]):
                                        result = layer.run_daily("2024-01-02")
            assert result is not None
            assert result.date == "2024-01-02"


class TestStrategyLayerHealth:
    def test_health_reports_actual_factor_count(self):
        """health() 必须报告引擎实例化的真实因子数, 而非空 ALL_FACTORS 属性 (回归: 曾恒为0)"""
        layer = StrategyLayer()
        with patch.object(layer, "_get_stock_codes"):
            health = layer.health()
        fc = None
        for c in health.checks:
            if c.name == "factor_engine":
                fc = c
                break
        assert fc is not None, "health() 必须包含 factor_engine 检查"
        assert fc.status == "pass", f"factor_engine 应为 pass, 实际: {fc.status} ({fc.detail})"
        assert "0 个因子" not in fc.detail, f"不应报告0因子: {fc.detail}"