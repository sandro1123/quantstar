"""Factor Evaluation 单元测试"""
import pytest
from backtest_layer.evaluation.factor_eval import (
    FactorEvaluator, FactorICResult, FactorTurnoverResult,
    FactorDecayResult, FactorCorrelation, FactorCorrelationResult,
)


class TestFactorIC:
    def test_compute_ic_basic(self):
        fe = FactorEvaluator()
        scores = [1.0, 2.0, 3.0, 4.0, 5.0]
        returns = [0.01, 0.02, 0.03, 0.04, 0.05]
        result = fe.compute_ic("momentum", scores, returns)
        assert result.valid is True
        assert result.ic > 0
        assert result.rank_ic > 0
        assert result.n_periods == 5

    def test_compute_ic_insufficient_data(self):
        fe = FactorEvaluator()
        result = fe.compute_ic("factor", [1.0], [0.01])
        assert result.valid is False

    def test_compute_ic_negative_correlation(self):
        fe = FactorEvaluator()
        scores = [5.0, 4.0, 3.0, 2.0, 1.0]
        returns = [0.01, 0.02, 0.03, 0.04, 0.05]
        result = fe.compute_ic("reverse", scores, returns)
        assert result.valid is True
        assert result.ic < 0

    def test_compute_ic_empty(self):
        fe = FactorEvaluator()
        result = fe.compute_ic("empty", [], [])
        assert result.valid is False

    def test_evaluate_multiple_factors(self):
        fe = FactorEvaluator()
        factor_scores = {
            "factor_a": [1.0, 2.0, 3.0, 4.0, 5.0],
            "factor_b": [5.0, 4.0, 3.0, 2.0, 1.0],
        }
        fwd_returns = [0.01, 0.02, 0.03, 0.04, 0.05]
        results = fe.evaluate(factor_scores, fwd_returns)
        assert "factor_a" in results
        assert "factor_b" in results
        assert results["factor_a"]["ic"] > 0
        assert results["factor_b"]["ic"] < 0


class TestFactorTurnover:
    def test_compute_turnover_basic(self):
        fe = FactorEvaluator()
        holdings = [["A", "B", "C"], ["B", "C", "D"], ["C", "D", "E"]]
        result = fe.compute_turnover("factor", holdings)
        assert result.avg_turnover > 0
        assert result.n_periods == 3

    def test_compute_turnover_no_change(self):
        fe = FactorEvaluator()
        holdings = [["A", "B"], ["A", "B"]]
        result = fe.compute_turnover("stable", holdings)
        assert result.avg_turnover == 0.0

    def test_compute_turnover_single_period(self):
        fe = FactorEvaluator()
        result = fe.compute_turnover("factor", [["A", "B"]])
        assert result.n_periods == 1


class TestFactorDecay:
    def test_compute_decay_basic(self):
        fe = FactorEvaluator()
        scores = list(range(1, 20))
        returns = [0.01 * i for i in range(1, 20)]
        result = fe.compute_decay("momentum", scores, returns, max_lag=5)
        assert len(result.decay_curve) == 6
        assert result.decay_curve[0] > 0

    def test_compute_decay_insufficient_data(self):
        fe = FactorEvaluator()
        result = fe.compute_decay("factor", [1.0], [0.01])
        assert len(result.decay_curve) == 0


class TestFactorCorrelation:
    def test_compute_basic(self):
        fc = FactorCorrelation()
        scores = {
            "factor_a": [1.0, 2.0, 3.0, 4.0, 5.0],
            "factor_b": [1.1, 2.1, 3.1, 4.1, 5.1],
            "factor_c": [5.0, 4.0, 3.0, 2.0, 1.0],
        }
        result = fc.compute(scores, threshold=0.7)
        assert len(result.factors) == 3
        assert len(result.matrix) == 3
        assert result.matrix[0][0] == 1.0
        assert len(result.high_corr_pairs) > 0

    def test_compute_empty(self):
        result = FactorCorrelation.compute({})
        assert len(result.factors) == 0

    def test_compute_no_high_correlation(self):
        fc = FactorCorrelation()
        scores = {
            "a": [1.0, 3.0, 2.0, 4.0, 3.5],
            "b": [2.0, 1.5, 3.0, 2.5, 1.0],
        }
        result = fc.compute(scores, threshold=0.95)
        assert len(result.high_corr_pairs) == 0
