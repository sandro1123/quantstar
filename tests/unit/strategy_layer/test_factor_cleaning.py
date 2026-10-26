"""Tests for factor cleaning utilities"""
import math
from strategy_layer.factors.factor_cleaning import mad_clip, zscore


class TestFactorCleaning:
    def test_mad_clip_empty(self):
        assert mad_clip([]) == []

    def test_mad_clip_preserves_normal_values(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        result = mad_clip(values, n_mad=5.0)
        assert result == values

    def test_mad_clip_clips_outliers(self):
        values = [1.0, 2.0, 3.0, 4.0, 100.0]
        result = mad_clip(values, n_mad=2.0)
        assert result[-1] < 100.0

    def test_mad_clip_handles_nan(self):
        values = [1.0, float("nan"), 3.0]
        result = mad_clip(values)
        assert len(result) == 3
        assert result[1] == 0.0

    def test_zscore_empty(self):
        assert zscore([]) == []

    def test_zscore_standardizes(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        result = zscore(values)
        assert abs(sum(result)) < 0.001
        assert abs(result[0] - (-1.2649)) < 0.01

    def test_zscore_constant_values(self):
        values = [3.0, 3.0, 3.0]
        result = zscore(values)
        assert all(v == 0.0 for v in result)

    def test_zscore_handles_nan(self):
        values = [1.0, float("nan"), 3.0]
        result = zscore(values)
        assert len(result) == 3
        assert result[1] == 0.0