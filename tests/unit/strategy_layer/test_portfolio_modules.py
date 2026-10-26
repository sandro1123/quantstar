"""Tests for portfolio optimization modules"""
import pytest
import numpy as np
from strategy_layer.portfolio.equal_weight import EqualWeightPortfolio
from strategy_layer.portfolio.hrp import HRP


class TestEqualWeight:
    def test_allocate_equal_weights(self):
        p = EqualWeightPortfolio(total_capital=100000.0)
        result = p.allocate(["000001", "000002", "000003"])
        assert len(result) == 3
        assert abs(result[0].weight - 1/3) < 0.001
        assert abs(result[0].estimated_value - 33333.33) < 1

    def test_empty_codes(self):
        p = EqualWeightPortfolio()
        assert p.allocate([]) == []


class TestHRP:
    def test_optimize_with_returns(self):
        p = HRP()
        returns = {"000001": np.array([0.01, 0.02, 0.03]),
                   "000002": np.array([0.02, 0.03, 0.04]),
                   "000003": np.array([0.03, 0.04, 0.05])}
        weights = p.optimize(returns)
        assert len(weights) == 3
        assert abs(sum(weights.values()) - 1.0) < 0.01