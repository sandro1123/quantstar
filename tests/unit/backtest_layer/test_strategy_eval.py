"""Strategy Evaluation 单元测试"""
import pytest
from backtest_layer.evaluation.strategy_eval import StrategyEvaluator, StrategyMetrics


class TestStrategyEvaluator:
    def test_evaluate_single_strategy(self):
        se = StrategyEvaluator()
        reports = [
            {"total_return": 0.05, "sharpe": 1.2, "max_drawdown": 0.03, "total_trades": 10, "trade_log": []},
            {"total_return": 0.03, "sharpe": 0.8, "max_drawdown": 0.05, "total_trades": 8, "trade_log": []},
        ]
        results = se.evaluate({"strategy_a": reports})
        assert "strategy_a" in results
        assert results["strategy_a"]["total_return"] == pytest.approx(0.08, abs=0.01)
        assert results["strategy_a"]["n_periods"] == 2

    def test_evaluate_empty(self):
        se = StrategyEvaluator()
        results = se.evaluate({})
        assert len(results) == 0

    def test_evaluate_single(self):
        se = StrategyEvaluator()
        reports = [{"total_return": 0.1, "sharpe": 1.5, "max_drawdown": 0.02, "trade_log": []}]
        results = se.evaluate({"solo": reports})
        assert results["solo"]["sharpe"] == 1.5

    def test_alpha_beta(self):
        se = StrategyEvaluator()
        strategy_returns = [0.01, 0.02, -0.01, 0.03, 0.01]
        benchmark_returns = [0.005, 0.01, -0.005, 0.015, 0.005]
        alpha, beta = se.alpha_beta(strategy_returns, benchmark_returns)
        assert isinstance(alpha, float)
        assert isinstance(beta, float)

    def test_compare(self):
        se = StrategyEvaluator()
        results = {
            "a": StrategyMetrics(name="a", sharpe=1.5, total_return=0.1, max_drawdown=0.05),
            "b": StrategyMetrics(name="b", sharpe=0.8, total_return=0.15, max_drawdown=0.03),
        }
        comp = se.compare(results)
        assert comp.best_sharpe["name"] == "a"
        assert comp.best_return["name"] == "b"
        assert comp.lowest_drawdown["name"] == "b"

    def test_monthly_returns(self):
        se = StrategyEvaluator()
        returns = [0.01] * 42
        monthly = se._monthly_returns(returns)
        assert len(monthly) == 2
        assert monthly[0]["period"] == "month_0"
