"""Tests for multi-objective optimization"""
import numpy as np
import pytest
from strategy_layer.orchestrator.allocator import (
    MultiObjectiveOptimizer,
    MultiObjectiveResult,
    OptimizationObjective,
)


class TestMultiObjectiveOptimizer:
    def test_compute_with_known_values(self):
        optimizer = MultiObjectiveOptimizer()
        returns = [0.001, 0.002, -0.001, 0.003, 0.001, -0.0005, 0.002, 0.001, -0.002, 0.001]
        win_rates = [0.6, 0.7, 0.5, 0.8, 0.55]
        weights = {"factor_a": 0.6, "factor_b": 0.4}

        result = optimizer.compute(returns, win_rates, weights)

        assert isinstance(result, MultiObjectiveResult)
        assert result.weights == weights
        assert result.win_rate == pytest.approx(0.63, abs=0.01)
        assert result.composite_score > 0
        assert result.sharpe != 0.0

    def test_compute_zero_std_returns(self):
        optimizer = MultiObjectiveOptimizer()
        returns = [0.0] * 10
        result = optimizer.compute(returns, [0.5], {"a": 1.0})

        assert result.sharpe == 0.0
        assert result.calmar == 0.0
        assert result.win_rate == 0.5

    def test_compute_empty_win_rates(self):
        optimizer = MultiObjectiveOptimizer()
        returns = [0.001, 0.002, -0.001]
        result = optimizer.compute(returns, [], {"a": 1.0})

        assert result.win_rate == 0.0
        assert result.composite_score >= 0

    def test_optimize_weights_with_ic_history(self):
        optimizer = MultiObjectiveOptimizer()
        ic_history = {
            "factor_a": [0.05, 0.06, 0.04, 0.07, 0.05, 0.06],
            "factor_b": [0.02, 0.01, 0.03, 0.02, 0.01, 0.02],
            "factor_c": [0.08, 0.07, 0.09, 0.08, 0.07, 0.08],
        }

        weights = optimizer.optimize_weights(ic_history)

        assert len(weights) == 3
        assert abs(sum(weights.values()) - 1.0) < 0.01
        assert weights["factor_c"] > weights["factor_b"]

    def test_optimize_weights_empty(self):
        optimizer = MultiObjectiveOptimizer()
        weights = optimizer.optimize_weights({})
        assert weights == {}

    def test_optimize_weights_single_factor(self):
        optimizer = MultiObjectiveOptimizer()
        weights = optimizer.optimize_weights({"factor_a": [0.05, 0.06, 0.04, 0.07, 0.05]})
        assert len(weights) == 1
        assert abs(weights["factor_a"] - 1.0) < 0.01

    def test_optimize_weights_insufficient_data(self):
        optimizer = MultiObjectiveOptimizer()
        weights = optimizer.optimize_weights({"factor_a": [0.05, 0.06]})
        assert len(weights) == 1
        assert abs(weights["factor_a"] - 1.0) < 0.01

    def test_custom_objectives(self):
        objectives = OptimizationObjective(sharpe_weight=0.5, calmar_weight=0.3, win_rate_weight=0.2)
        optimizer = MultiObjectiveOptimizer(objectives)
        returns = [0.001, 0.002, -0.001, 0.003]
        result = optimizer.compute(returns, [0.6], {"a": 1.0})

        expected = 0.5 * max(0, result.sharpe) + 0.3 * max(0, result.calmar) + 0.2 * 0.6
        assert result.composite_score == pytest.approx(expected, abs=0.01)