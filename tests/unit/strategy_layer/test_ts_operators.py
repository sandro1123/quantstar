"""Tests for time-series operators"""
import math
from strategy_layer.operator_lib.ts_operators import (
    ts_sum, ts_rank, ts_stddev, ts_corr, ts_covariance,
    ts_zscore, ts_min, ts_max, ts_argmax, ts_argmin,
    ts_skew, ts_kurt, ts_decaylinear, ts_delay, ts_delta,
)


class TestTsOperators:
    def test_ts_sum(self):
        assert ts_sum([1, 2, 3, 4, 5], 3) == 12
        assert ts_sum([], 5) == 0.0
        assert ts_sum([1, 2], 0) == 0.0

    def test_ts_rank(self):
        r = ts_rank([1, 2, 3, 4, 5], 5)
        assert r == 1.0
        r = ts_rank([5, 4, 3, 2, 1], 5)
        assert r == 0.0

    def test_ts_stddev(self):
        std = ts_stddev([1, 1, 1, 1, 1], 5)
        assert std == 0.0
        std = ts_stddev([1, 2, 3, 4, 5], 5)
        assert std > 0.0

    def test_ts_corr(self):
        a = [1, 2, 3, 4, 5]
        b = [2, 4, 6, 8, 10]
        c = ts_corr(a, b, 5)
        assert abs(c - 1.0) < 0.001
        c = ts_corr(a, [5, 4, 3, 2, 1], 5)
        assert abs(c + 1.0) < 0.001

    def test_ts_corr_short_window(self):
        assert ts_corr([1, 2], [3, 4], 5) == 0.0

    def test_ts_zscore(self):
        z = ts_zscore([1, 1, 1, 1, 1], 5)
        assert z == 0.0
        z = ts_zscore([1, 2, 3, 4, 10], 5)
        assert z > 0

    def test_ts_min_max(self):
        assert ts_min([3, 1, 4, 1, 5], 5) == 1.0
        assert ts_max([3, 1, 4, 1, 5], 5) == 5.0

    def test_ts_argmax_argmin(self):
        assert ts_argmax([3, 1, 4, 1, 5], 5) == 4
        assert ts_argmin([3, 1, 4, 1, 5], 5) == 1

    def test_ts_delay(self):
        assert ts_delay([1, 2, 3, 4, 5], 1) == 4
        assert ts_delay([1, 2, 3, 4, 5], 3) == 2
        assert ts_delay([1], 1) == 0.0

    def test_ts_delta(self):
        assert ts_delta([1, 2, 3, 4, 5], 1) == 1.0
        assert ts_delta([1, 2, 3, 4, 5], 3) == 3.0

    def test_ts_decaylinear(self):
        d = ts_decaylinear([1, 1, 1, 1, 1], 5)
        assert d == 1.0
        d = ts_decaylinear([1, 2, 3, 4, 5], 5)
        assert d > 3.0

    def test_ts_skew(self):
        s = ts_skew([1, 1, 1, 1, 1], 5)
        assert s == 0.0
        s = ts_skew([1, 2, 3], 3)
        assert abs(s) < 0.001

    def test_empty_returns_zero(self):
        assert ts_sum([], 5) == 0.0
        assert ts_stddev([], 5) == 0.0
        assert ts_min([], 5) == 0.0
        assert ts_max([], 5) == 0.0
        assert ts_argmax([], 5) == -1
        assert ts_argmin([], 5) == -1