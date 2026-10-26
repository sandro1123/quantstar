"""Walk-Forward Mode 单元测试"""
import pytest
from unittest.mock import MagicMock
from backtest_layer.core.broker import Broker
from backtest_layer.core.models import BacktestInput, CostConfig
from backtest_layer.modes.walk_forward import WalkForwardMode, WalkForwardConfig


class TestWalkForwardConfig:
    def test_default_config(self):
        cfg = WalkForwardConfig()
        assert cfg.n_windows == 5
        assert cfg.min_window_days == 60
        assert cfg.anchored is False
        assert cfg.is_ratio == 0.7

    def test_custom_config(self):
        cfg = WalkForwardConfig(n_windows=3, anchored=True)
        assert cfg.n_windows == 3
        assert cfg.anchored is True


class TestWalkForwardMode:
    def _make_wf(self, n_windows=3, anchored=False):
        broker = Broker(CostConfig(initial_capital=100000))
        data_layer = MagicMock()
        strategy_layer = MagicMock()
        cfg = WalkForwardConfig(n_windows=n_windows, anchored=anchored, min_window_days=10)
        return WalkForwardMode(broker, data_layer, strategy_layer, cfg)

    def test_run_basic(self):
        wf = self._make_wf(n_windows=3)
        inp = BacktestInput(mode="walk_forward", codes=["000001"], date_range=("2026-01-01", "2026-12-31"))
        report = wf.run(inp)
        assert report.mode == "walk_forward"
        assert report.wf_result is not None
        assert report.wf_result["window_count"] == 3

    def test_run_anchored(self):
        wf = self._make_wf(n_windows=3, anchored=True)
        inp = BacktestInput(mode="walk_forward", codes=["000001"], date_range=("2026-01-01", "2026-12-31"))
        report = wf.run(inp)
        assert report.wf_result["anchored"] is True

    def test_run_too_short(self):
        wf = self._make_wf()
        inp = BacktestInput(mode="walk_forward", codes=["000001"], date_range=("2026-01-01", "2026-01-10"))
        report = wf.run(inp)
        assert "error" in report.wf_result

    def test_run_invalid_date(self):
        wf = self._make_wf()
        inp = BacktestInput(mode="walk_forward", codes=["000001"], date_range=("invalid", "2026-01-10"))
        report = wf.run(inp)
        assert "error" in report.wf_result

    def test_stability_score(self):
        wf = self._make_wf()
        score = wf._stability_score([0.05, 0.03, 0.04, 0.02])
        assert 0 <= score <= 1.0

    def test_stability_score_single(self):
        wf = self._make_wf()
        score = wf._stability_score([0.05])
        assert score == 1.0

    def test_stability_score_empty(self):
        wf = self._make_wf()
        score = wf._stability_score([])
        assert score == 1.0
