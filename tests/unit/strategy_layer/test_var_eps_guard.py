"""刀4案2⑦ 标量侧退化方差守卫修复 — 红绿测试 (统筹特批条件 a)。

红: 修复前常数序列进入 0/0 未定义区, 输出 O(1) 垃圾值 (非 0.0)。
绿: 修复后 var/std < 1e-12 守卫触发, 常数序列输出定义值 0.0。

场景 = A股一字板 (连续同价 bar), 真实高频场景。
"""
import math

import pytest

from strategy_layer.operator_lib import ts_operators as ts


CONST = [7.7] * 250          # 一字板: 全同价
CONST_SHORT = [3.3] * 10     # 短常数序列


class TestDegenerateVarianceGuard:
    """常数序列 (浮点舍入使 var~1e-30 非精确 0) 必须输出定义值 0.0。"""

    def test_ts_zscore_constant_is_zero(self):
        for i in range(len(CONST)):
            v = ts.ts_zscore(CONST[: i + 1], 20)
            assert v == 0.0, "bar %d: zscore=%r (垃圾值, 守卫漏触发)" % (i, v)

    def test_ts_skew_constant_is_zero(self):
        for i in range(len(CONST)):
            v = ts.ts_skew(CONST[: i + 1], 20)
            assert v == 0.0, "bar %d: skew=%r" % (i, v)

    def test_ts_kurt_constant_is_zero(self):
        for i in range(len(CONST)):
            v = ts.ts_kurt(CONST[: i + 1], 20)
            assert v == 0.0, "bar %d: kurt=%r" % (i, v)

    def test_ts_corr_constant_b_is_zero(self):
        a = [float(i % 7) for i in range(len(CONST))]
        for i in range(9, len(CONST)):
            v = ts.ts_corr(a[: i + 1], CONST[: i + 1], 10)
            assert v == 0.0, "bar %d: corr=%r" % (i, v)

    def test_ts_corr_both_constant_is_zero(self):
        for i in range(9, len(CONST)):
            v = ts.ts_corr(CONST[: i + 1], CONST[: i + 1], 10)
            assert v == 0.0, "bar %d: corr=%r" % (i, v)


class TestLegitimateVarianceUnaffected:
    """1e-12 阈值不误伤合法小方差序列 (价格尺度 var~1e-4, 收益率 ~1e-6)。"""

    def test_zscore_small_variance_still_computes(self):
        # 微小波动 (std~1e-3, var~1e-6 >> 1e-12) 必须正常计算非零
        vals = [100.0 + 0.001 * ((i % 5) - 2) for i in range(50)]
        v = ts.ts_zscore(vals, 20)
        assert v != 0.0
        assert math.isfinite(v)

    def test_corr_small_variance_still_computes(self):
        a = [100.0 + 0.001 * math.sin(i) for i in range(50)]
        b = [50.0 + 0.001 * math.cos(i) for i in range(50)]
        v = ts.ts_corr(a, b, 20)
        assert math.isfinite(v)
        assert -1.0 <= v <= 1.0

    def test_skew_kurt_normal_sequence_unchanged(self):
        import random
        rng = random.Random(42)
        vals = [rng.gauss(0, 1) for _ in range(60)]
        sk = ts.ts_skew(vals, 20)
        ku = ts.ts_kurt(vals, 20)
        assert math.isfinite(sk) and math.isfinite(ku)
        # 正常序列守卫不触发, 值与手算一致
        recent = vals[-20:]
        mean = sum(recent) / 20
        std = math.sqrt(sum((x - mean) ** 2 for x in recent) / 20)
        assert abs(sk - sum((x - mean) ** 3 for x in recent) / (20 * std ** 3)) < 1e-12

    def test_exact_zero_variance_still_zero(self):
        # 精确零方差 (全 0 序列) 行为不变
        assert ts.ts_zscore([0.0] * 30, 20) == 0.0
        assert ts.ts_skew([0.0] * 30, 20) == 0.0
        assert ts.ts_kurt([0.0] * 30, 20) == 0.0
        assert ts.ts_corr([0.0] * 30, [1.0, 2.0] * 15, 20) == 0.0
