"""Tests for IC Calculator — IC 显著性 + 因子自相关 (G13, G14)"""

import numpy as np
import pytest

from strategy_layer.aggregation.ic_calculator import ICCalculator, ICResult


class TestICSignificance:
    def setup_method(self):
        self.calc = ICCalculator()

    def test_significance_known_positive(self):
        """Perfect positive rank correlation should yield p ≈ 0, t_stat large"""
        x = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
        y = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
        result = self.calc.compute_ic_with_significance(x, y)
        assert isinstance(result, ICResult)
        assert result.mean_ic == pytest.approx(1.0, abs=1e-6)
        assert result.is_significant
        assert result.sample_size == 10

    def test_significance_known_negative(self):
        """Perfect negative rank correlation"""
        x = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
        y = [10.0, 9.0, 8.0, 7.0, 6.0, 5.0, 4.0, 3.0, 2.0, 1.0]
        result = self.calc.compute_ic_with_significance(x, y)
        assert result.mean_ic == pytest.approx(-1.0, abs=1e-6)
        assert result.is_significant

    def test_significance_no_correlation(self):
        """Random noise should yield p > 0.05"""
        rng = np.random.default_rng(42)
        x = rng.normal(size=100).tolist()
        y = rng.normal(size=100).tolist()
        result = self.calc.compute_ic_with_significance(x, y)
        assert not result.is_significant
        assert result.sample_size == 100

    def test_significance_small_sample(self):
        """Fewer than 10 samples should return neutral result"""
        result = self.calc.compute_ic_with_significance([1.0, 2.0, 3.0], [1.0, 2.0, 3.0])
        assert result.mean_ic == 0.0
        assert not result.is_significant
        assert result.sample_size == 3

    def test_significance_with_nan(self):
        """NaN values should be masked out"""
        x = [1.0, 2.0, np.nan, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0, 13.0]
        y = [1.0, 2.0, 3.0, np.nan, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0, 13.0]
        result = self.calc.compute_ic_with_significance(x, y)
        assert result.sample_size == 11  # 13 - 2 NaN
        assert result.mean_ic == pytest.approx(1.0, abs=1e-6)


class TestFactorRankAutocorrelation:
    def setup_method(self):
        self.calc = ICCalculator()

    def test_autocorrelation_perfect_stability(self):
        """Same rank each day → autocorrelation = 1.0"""
        data = {
            "2024-01-01": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0],
            "2024-01-02": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0],
            "2024-01-03": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0],
        }
        result = self.calc.factor_rank_autocorrelation(data)
        assert result["mean_autocorrelation"] == pytest.approx(1.0, abs=1e-6)
        assert result["n_periods"] == 2

    def test_autocorrelation_reversed(self):
        """Reversed rank each day → autocorrelation = -1.0"""
        data = {
            "2024-01-01": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0],
            "2024-01-02": [11.0, 10.0, 9.0, 8.0, 7.0, 6.0, 5.0, 4.0, 3.0, 2.0, 1.0],
        }
        result = self.calc.factor_rank_autocorrelation(data)
        assert result["mean_autocorrelation"] == pytest.approx(-1.0, abs=1e-6)
        assert result["n_periods"] == 1

    def test_autocorrelation_single_day(self):
        """Single date → no periods → 0.0"""
        result = self.calc.factor_rank_autocorrelation({"2024-01-01": [1.0, 2.0, 3.0]})
        assert result["mean_autocorrelation"] == 0.0
        assert result["n_periods"] == 0


class TestStrategyIC:
    def setup_method(self):
        self.calc = ICCalculator()

    def test_strategy_ic_known_positive(self):
        scores = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
        returns = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
        factor_ics = [
            {"factor_name": "factor_a", "mean_ic": 0.12},
            {"factor_name": "factor_b", "mean_ic": 0.08},
        ]
        result = self.calc.compute_strategy_ic(scores, returns, factor_ics)
        assert result["strategy_ic"] == pytest.approx(1.0, abs=1e-4)
        assert result["is_significant"]
        assert result["sample_size"] == 10
        assert result["factor_count"] == 2

    def test_strategy_ic_factor_contributions(self):
        scores = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
        returns = [10.0, 9.0, 8.0, 7.0, 6.0, 5.0, 4.0, 3.0, 2.0, 1.0]
        factor_ics = [
            {"factor_name": "weak", "mean_ic": 0.01},
            {"factor_name": "strong", "mean_ic": 0.25},
            {"factor_name": "medium", "mean_ic": 0.12},
        ]
        result = self.calc.compute_strategy_ic(scores, returns, factor_ics)
        assert result["factor_count"] == 3
        assert result["top_factors"][0]["factor"] == "strong"

    def test_strategy_ic_small_sample(self):
        result = self.calc.compute_strategy_ic([1.0, 2.0, 3.0], [1.0, 2.0, 3.0], [])
        assert result["strategy_ic"] == 0.0
        assert not result["is_significant"]
        assert result["sample_size"] == 3

    def test_strategy_ic_with_nan(self):
        scores = [1.0, np.nan, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0, 13.0]
        returns = [1.0, 2.0, np.nan, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0, 13.0]
        factor_ics = [{"factor_name": "f1", "mean_ic": 0.1}]
        result = self.calc.compute_strategy_ic(scores, returns, factor_ics)
        assert result["sample_size"] == 11
        assert result["factor_count"] == 1


class TestFactorRankAutocorrelation:
    def setup_method(self):
        self.calc = ICCalculator()

    def test_autocorrelation_small_sample_filtered(self):
        """Fewer than 10 stocks per period → filtered out"""
        data = {
            "2024-01-01": [1.0, 2.0, 3.0, 4.0, 5.0],
            "2024-01-02": [5.0, 4.0, 3.0, 2.0, 1.0],
        }
        result = self.calc.factor_rank_autocorrelation(data)
        assert result["n_periods"] == 0
        assert result["mean_autocorrelation"] == 0.0