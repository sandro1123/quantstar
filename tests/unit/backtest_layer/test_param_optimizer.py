"""Parameter Optimization 单元测试"""
import pytest
from unittest.mock import MagicMock
from backtest_layer.feedback.param_tuner import (
    GridSearch, RandomSearch, BayesianOptimizer, OptimizationResult,
)


class TestGridSearch:
    def test_grid_search_basic(self):
        mock_engine = MagicMock()
        mock_report = MagicMock()
        mock_report.sharpe = 1.5
        mock_report.total_return = 0.1
        mock_report.max_drawdown = 0.05
        mock_engine.run.return_value = mock_report
        from backtest_layer.core.models import BacktestInput
        base_input = BacktestInput(mode="hold", codes=["000001"], date_range=("2026-01-01", "2026-06-30"))
        gs = GridSearch(backtest_engine=mock_engine)
        result = gs.search(
            {"commission_rate": [0.0001, 0.0003], "slippage_rate": [0.001]},
            base_input,
        )
        assert result.status == "success"
        assert result.method == "grid"
        assert result.best_sharpe == 1.5
        assert result.n_evaluations == 2

    def test_grid_search_empty(self):
        mock_engine = MagicMock()
        mock_report = MagicMock()
        mock_report.sharpe = 0.5
        mock_report.total_return = 0.01
        mock_report.max_drawdown = 0.01
        mock_engine.run.return_value = mock_report
        from backtest_layer.core.models import BacktestInput
        base_input = BacktestInput(mode="hold", codes=[], date_range=("2026-01-01", "2026-06-30"))
        gs = GridSearch(backtest_engine=mock_engine)
        result = gs.search({"param": [1]}, base_input)
        assert result.n_evaluations >= 0


class TestRandomSearch:
    def test_random_search_basic(self):
        mock_engine = MagicMock()
        mock_report = MagicMock()
        mock_report.sharpe = 1.2
        mock_report.total_return = 0.08
        mock_report.max_drawdown = 0.04
        mock_engine.run.return_value = mock_report
        from backtest_layer.core.models import BacktestInput
        base_input = BacktestInput(mode="hold", codes=["000001"], date_range=("2026-01-01", "2026-06-30"))
        rs = RandomSearch(backtest_engine=mock_engine)
        result = rs.search(
            {"commission_rate": [0.0001, 0.0003], "slippage_rate": [0.001, 0.002]},
            base_input,
            n_trials=5,
        )
        assert result.status == "success"
        assert result.method == "random"
        assert result.n_evaluations <= 5


class TestBayesianOptimizer:
    def test_bayesian_basic(self):
        mock_engine = MagicMock()
        mock_report = MagicMock()
        mock_report.sharpe = 1.0
        mock_report.total_return = 0.05
        mock_report.max_drawdown = 0.03
        mock_engine.run.return_value = mock_report
        from backtest_layer.core.models import BacktestInput
        base_input = BacktestInput(mode="hold", codes=["000001"], date_range=("2026-01-01", "2026-06-30"))
        bo = BayesianOptimizer(backtest_engine=mock_engine, n_initial=3, n_iterations=5)
        result = bo.search(
            {"commission_rate": [0.0001, 0.0003], "slippage_rate": [0.001]},
            base_input,
        )
        assert result.status == "success"
        assert result.method == "bayesian"
        assert result.n_evaluations == 8

    def test_bayesian_exception_handling(self):
        mock_engine = MagicMock()
        mock_engine.run.side_effect = RuntimeError("fail")
        from backtest_layer.core.models import BacktestInput
        base_input = BacktestInput(mode="hold", codes=["000001"], date_range=("2026-01-01", "2026-06-30"))
        bo = BayesianOptimizer(backtest_engine=mock_engine, n_initial=2, n_iterations=2)
        result = bo.search({"p": [1, 2]}, base_input)
        assert result.status == "success"
        assert result.n_evaluations == 0
