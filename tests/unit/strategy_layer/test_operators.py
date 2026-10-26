"""Tests for operators library (30+ operators)"""
import math
import pytest
from strategy_layer.operator_lib import (
    ts_sum, ts_rank, ts_stddev, ts_corr, ts_covariance, ts_zscore,
    ts_min, ts_max, ts_argmax, ts_argmin, ts_skew, ts_kurt,
    ts_decaylinear, ts_wma, ts_delay, ts_delta,
    rank, scale, clip, demean,
    group_rank, group_scale, group_zscore, group_sum, group_mean,
    abs_, log, sign, neg, not_,
    signedpower, abs_op, log_op, sqrt_op, if_else,
)


class TestTSOperators:
    def test_ts_sum(self):
        assert ts_sum([1, 2, 3, 4, 5], 3) == 12.0
        assert ts_sum([], 5) == 0.0

    def test_ts_rank(self):
        assert 0.0 <= ts_rank([1, 2, 3, 4, 5], 5) <= 1.0
        assert ts_rank([1, 2, 3, 4, 5], 5) == 1.0
        assert ts_rank([], 5) == 0.0

    def test_ts_stddev(self):
        result = ts_stddev([1, 2, 3, 4, 5], 5)
        assert result == pytest.approx(math.sqrt(2.0))
        assert ts_stddev([], 5) == 0.0
        assert ts_stddev([1], 1) == 0.0

    def test_ts_corr(self):
        a = [1, 2, 3, 4, 5]
        b = [2, 4, 6, 8, 10]
        assert ts_corr(a, b, 5) == pytest.approx(1.0)
        assert ts_corr([1, 2], [3, 4], 5) == 0.0

    def test_ts_covariance(self):
        a = [1, 2, 3, 4, 5]
        b = [2, 4, 6, 8, 10]
        assert ts_covariance(a, b, 5) == pytest.approx(4.0)
        assert ts_covariance([1, 2], [3, 4], 5) == 0.0

    def test_ts_zscore(self):
        vals = [1, 2, 3, 4, 10]
        z = ts_zscore(vals, 5)
        assert z > 0
        assert ts_zscore([1, 1, 1], 3) == 0.0
        assert ts_zscore([], 5) == 0.0

    def test_ts_min(self):
        assert ts_min([3, 1, 4, 1, 5], 3) == 1.0
        assert ts_min([], 5) == 0.0

    def test_ts_max(self):
        assert ts_max([3, 1, 4, 1, 5], 3) == 5.0
        assert ts_max([], 5) == 0.0

    def test_ts_argmax(self):
        assert ts_argmax([1, 3, 2, 5, 4], 5) == 3
        assert ts_argmax([], 5) == -1

    def test_ts_argmin(self):
        assert ts_argmin([3, 1, 4, 1, 5], 5) == 1
        assert ts_argmin([], 5) == -1

    def test_ts_skew(self):
        vals = [1, 2, 3, 4, 100]
        skew = ts_skew(vals, 5)
        assert skew > 0
        assert ts_skew([1, 1, 1, 1], 4) == 0.0
        assert ts_skew([1, 2], 2) == 0.0

    def test_ts_kurt(self):
        vals = [1, 2, 3, 4, 5]
        kurt = ts_kurt(vals, 5)
        assert kurt < 0
        assert ts_kurt([1, 2, 3], 3) == 0.0
        assert ts_kurt([1, 2], 2) == 0.0

    def test_ts_decaylinear(self):
        vals = [1, 1, 1, 1, 1]
        assert ts_decaylinear(vals, 5) == 1.0
        vals2 = [1, 2, 3]
        dl = ts_decaylinear(vals2, 3)
        assert dl > 2.0
        assert ts_decaylinear([], 5) == 0.0

    def test_ts_wma(self):
        assert ts_wma([1, 1, 1], 3) == 1.0
        assert ts_wma([1, 2, 3], 3) == ts_decaylinear([1, 2, 3], 3)

    def test_ts_delay(self):
        assert ts_delay([1, 2, 3, 4, 5], 1) == 4.0
        assert ts_delay([1, 2, 3], 5) == 0.0
        assert ts_delay([1], 0) == 1.0

    def test_ts_delta(self):
        assert ts_delta([1, 2, 3, 4, 5], 1) == 1.0
        assert ts_delta([1, 2, 3], 5) == 0.0
        assert ts_delta([5, 5], 1) == 0.0


class TestCSScalarOperators:
    def test_rank(self):
        assert 0.0 <= rank([1, 2, 3, 4, 5]) <= 1.0
        assert rank([1, 2, 3, 4, 5]) == 1.0
        assert rank([]) == 0.0

    def test_scale(self):
        assert scale([1, 2, 3, 4], 1.0) == pytest.approx(0.4)
        assert scale([], 1.0) == 0.0
        assert scale([0, 0, 0], 1.0) == 0.0

    def test_clip(self):
        assert clip(5.0, -1.0, 1.0) == 1.0
        assert clip(-5.0, -1.0, 1.0) == -1.0
        assert clip(0.5, -1.0, 1.0) == 0.5

    def test_demean(self):
        assert demean([1, 2, 3]) == 1.0
        assert demean([1, 2, 6]) == 3.0
        assert demean([]) == 0.0


class TestHelperScalarOperators:
    def test_abs_(self):
        assert abs_(-5.0) == 5.0
        assert abs_(3.0) == 3.0
        assert abs_(0.0) == 0.0

    def test_log(self):
        assert log(1.0) == 0.0
        assert log(0.0) == 0.0
        assert log(-1.0) == 0.0
        assert log(math.e) == pytest.approx(1.0)

    def test_sign(self):
        assert sign(5.0) == 1.0
        assert sign(-3.0) == -1.0
        assert sign(0.0) == 0.0

    def test_neg(self):
        assert neg(5.0) == -5.0
        assert neg(-3.0) == 3.0
        assert neg(0.0) == 0.0

    def test_not_(self):
        assert not_(0.0) == 1.0
        assert not_(1.0) == 0.0
        assert not_(-1.0) == 0.0


class TestCSOperators:
    def test_group_rank_no_groups(self):
        result = group_rank([3, 1, 4, 2])
        assert len(result) == 4
        assert all(0 <= r <= 1 for r in result)
        assert result[0] > result[1]

    def test_group_rank_with_groups(self):
        result = group_rank([10, 20, 30, 40], groups=["a", "a", "b", "b"])
        assert len(result) == 4
        assert result[0] < result[1]
        assert result[2] < result[3]

    def test_group_scale_no_groups(self):
        result = group_scale([1, 2, 3, 4, 5])
        assert len(result) == 5
        assert sum(result) == pytest.approx(0.0, abs=1e-10)
        # Constant input
        result2 = group_scale([1, 1, 1])
        assert all(r == 0.0 for r in result2)

    def test_group_scale_with_groups(self):
        result = group_scale([10, 20, 30, 40], groups=["a", "a", "b", "b"])
        assert len(result) == 4

    def test_group_zscore(self):
        assert group_zscore([1, 2, 3]) == group_scale([1, 2, 3])

    def test_group_sum(self):
        result = group_sum([1, 2, 3, 4], groups=["a", "a", "b", "b"])
        assert result[0] == 3.0
        assert result[1] == 3.0
        assert result[2] == 7.0
        assert result[3] == 7.0

    def test_group_mean(self):
        result = group_mean([1, 2, 3, 4], groups=["a", "a", "b", "b"])
        assert result[0] == 1.5
        assert result[1] == 1.5
        assert result[2] == 3.5
        assert result[3] == 3.5


class TestHelperOps:
    def test_signedpower(self):
        result = signedpower([-2, -1, 0, 1, 2], 2.0)
        assert result == [-4.0, -1.0, 0.0, 1.0, 4.0]

    def test_abs_op(self):
        assert abs_op([-3, -1, 0, 2, 4]) == [3.0, 1.0, 0.0, 2.0, 4.0]

    def test_log_op(self):
        result = log_op([1, 10, 100])
        assert result[0] == pytest.approx(0.0)
        assert result[1] == pytest.approx(math.log(10))
        assert result[2] == pytest.approx(math.log(100))

    def test_sqrt_op(self):
        result = sqrt_op([0, 4, 9, 16])
        assert result == [0.0, 2.0, 3.0, 4.0]
        # Negative values
        result2 = sqrt_op([-4, -9])
        assert result2 == [2.0, 3.0]

    def test_if_else(self):
        cond = [True, False, True, False]
        t = [1.0, 2.0, 3.0, 4.0]
        f = [10.0, 20.0, 30.0, 40.0]
        result = if_else(cond, t, f)
        assert result == [1.0, 20.0, 3.0, 40.0]


class TestEdgeCases:
    def test_empty_inputs(self):
        assert ts_sum([], 5) == 0.0
        assert ts_rank([], 5) == 0.0
        assert ts_stddev([], 5) == 0.0
        assert ts_min([], 5) == 0.0
        assert ts_max([], 5) == 0.0
        assert ts_argmax([], 5) == -1
        assert ts_argmin([], 5) == -1
        assert ts_skew([], 5) == 0.0
        assert ts_kurt([], 5) == 0.0
        assert ts_decaylinear([], 5) == 0.0
        assert group_rank([]) == []
        assert group_scale([]) == []
        assert abs_op([]) == []
        assert log_op([]) == []
        assert sqrt_op([]) == []

    def test_nan_handling(self):
        vals = [1.0, float("nan"), 3.0]
        result = ts_sum(vals, 3)
        assert math.isnan(result)
        result2 = abs_op(vals)
        assert not any(math.isnan(v) for v in result2 if not math.isnan(v))
        assert math.isnan(result2[1])