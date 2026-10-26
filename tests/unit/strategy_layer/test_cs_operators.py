"""Tests for cross-sectional and helper operators"""
from strategy_layer.operator_lib.cs_operators import rank, scale, clip, demean
from strategy_layer.operator_lib.helper_ops import if_else, sign, stddev, delay, delta


class TestCSScalarOperators:
    def test_rank(self):
        assert rank([1, 2, 3, 4, 5]) == 1.0
        assert rank([5, 4, 3, 2, 1]) == 0.0
        assert rank([3, 1, 4, 1, 5]) > 0.5
        assert rank([]) == 0.0

    def test_scale(self):
        assert scale([1, 2, 3, 4, 5], 1.0) == 1/3
        assert scale([], 1.0) == 0.0
        assert scale([0, 0, 0], 1.0) == 0.0

    def test_clip(self):
        assert clip(0.5) == 0.5
        assert clip(-2.0) == -1.0
        assert clip(2.0) == 1.0
        assert clip(0.5, 0, 1) == 0.5
        assert clip(-0.5, 0, 1) == 0.0

    def test_demean(self):
        assert demean([1, 2, 3, 4, 5]) == 2.0
        assert demean([5, 4, 3, 2, 1]) == -2.0
        assert demean([]) == 0.0


class TestHelperOps:
    def test_if_else(self):
        import math
        result = if_else([True, False], [1.0, 2.0], [3.0, 4.0])
        assert result == [1.0, 4.0]

    def test_sign(self):
        assert sign(5) == 1
        assert sign(-5) == -1
        assert sign(0) == 0

    def test_stddev(self):
        s = stddev([1, 1, 1, 1, 1], 5)
        assert s == 0.0
        s = stddev([1, 2, 3, 4, 5], 5)
        assert s > 0.0

    def test_delay(self):
        assert delay([1, 2, 3, 4, 5], 1) == 4
        assert delay([1], 1) == 0.0

    def test_delta(self):
        assert delta([1, 2, 3, 4, 5], 1) == 1.0