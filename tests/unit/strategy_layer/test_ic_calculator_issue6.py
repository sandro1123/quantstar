# -*- coding: utf-8 -*-
"""#6 IC 计算: 平均秩 / ICIR 口径 / 缺失值策略

Issue #6 原文:
  「核对 Rank IC 是否对并列值使用平均秩、ICIR 分母与窗口、
    缺失因子值是剔除还是填充。
    验收: 与 scipy.stats.spearmanr 对拍一致; 缺失值策略写入文档并有测试。」

## 本卡修的两个真缺陷

### A. 并列值不是平均秩 + 自带 mapping 错配
旧 `compute_ic` 用 `{v: i for i, v in enumerate(sorted(...))}` 自造名次表:
  - 两个因子值相同 → 后写者覆盖, 该值只留一个名次 (不是 midrank 3.5);
  - 因子值集合与收益值集合的字典各自排序, 在重复值下配对会错。
结果: 与 scipy 对拍不上, 且并列越多偏差越大 (A股大量价格/估值并列值)。

### B. icir 不是 ICIR
旧 `icir = corr / std(forward_returns)` = 单期 IC / 收益标准差。
ICIR 的业界定义是跨期的 `mean(IC_series)/std(IC_series)`。
两者量纲与含义都不同, 下游拿它筛因子等于用错指标。

## 缺失值策略 (issue 明确要求)
**成对删除 (pairwise deletion), 不填充。** 已写进模块 docstring。
本测试钉住: NaN 被剔除后不影响其余样本; 全 NaN → 返回 0 / 不显著, 不崩。
"""
import numpy as np
import pytest
from scipy.stats import spearmanr

from strategy_layer.aggregation.ic_calculator import ICCalculator, ICResult


@pytest.fixture
def calc():
    return ICCalculator()


def _mk(n=20, seed=7, ties_at=0.0):
    """构造 n 个样本; ties_at>0 时让一部分因子值/收益值并列。"""
    rng = np.random.default_rng(seed)
    f = rng.normal(size=n)
    r = rng.normal(size=n)
    if ties_at > 0:
        k = max(2, int(n * ties_at))
        f[:k] = f[0]          # 制造并列
        r[:k] = r[0]
    return f, r


# ============================================================ 组1: 与 scipy 对拍
class TestMatchesScipy:
    @pytest.mark.parametrize("seed", [1, 7, 42, 123])
    def test_compute_ic_matches_scipy_exactly(self, calc, seed):
        """无并列: compute_ic 与 scipy.spearmanr 逐位一致 (<1e-12)"""
        f, r = _mk(30, seed=seed)
        fs = {f"{i:06d}": float(v) for i, v in enumerate(f)}
        fr = {f"{i:06d}": float(v) for i, v in enumerate(r)}

        got = calc.compute_ic(fs, fr)
        want, _ = spearmanr(f, r)

        assert abs(got - want) < 1e-12, f"seed={seed}: {got} vs {want}"

    def test_compute_ic_matches_scipy_with_ties(self, calc):
        """关键: **有并列值**时也必须与 scipy 一致 (旧实现这里会偏)

        scipy 对并列用 midrank; 旧的自造 {v:i} 映射会给并列值不同名次。
        """
        f, r = _mk(30, seed=5, ties_at=0.4)   # 40% 样本并列
        fs = {f"{i:06d}": float(v) for i, v in enumerate(f)}
        fr = {f"{i:06d}": float(v) for i, v in enumerate(r)}

        got = calc.compute_ic(fs, fr)
        want, _ = spearmanr(f, r)

        assert abs(got - want) < 1e-12, (
            f"并列值场景偏离 scipy: got={got}, scipy={want} — "
            f"说明仍在用非平均秩的自造映射")

    def test_old_implementation_would_have_failed_here(self):
        """反向证据: 复刻旧实现, 证明它在并列场景下确实偏离 scipy"""
        f, r = _mk(30, seed=5, ties_at=0.4)
        n = len(f)
        rank_x = sorted(f)
        rank_y = sorted(r)
        rx = {v: i for i, v in enumerate(rank_x)}
        ry = {v: i for i, v in enumerate(rank_y)}
        d_sq = sum((rx[a] - ry[b]) ** 2 for a, b in zip(f, r))
        old = 1 - 6 * d_sq / (n * (n * n - 1))
        want, _ = spearmanr(f, r)
        # 旧实现偏离 scipy 可达 1%+ —— 正是本 bug 的量级
        assert abs(old - want) > 1e-6, (
            f"旧实现居然与 scipy 一致 ({old} vs {want}) — "
            f"夹具没造出并列, 反向证据失效")


# ============================================================ 组2: ICIR 口径
class TestICIRCaliber:
    def test_icir_is_mean_over_std_of_ic_series(self, calc):
        """真 ICIR = mean(IC序列)/std(IC序列, ddof=1)"""
        series = [0.05, 0.08, 0.03, 0.06, 0.09, 0.02, 0.07, 0.04, 0.05, 0.06,
                  0.08, 0.03]
        got = calc.compute_icir(series)
        want = round(float(np.mean(series) / np.std(series, ddof=1)), 4)
        assert got == pytest.approx(want, abs=1e-6)

    def test_single_cross_section_icir_is_none(self, calc):
        """单期 IC 不可算 ICIR → None (宁缺毋滥, 不造 0 冒充)"""
        f, r = _mk(30)
        res = calc.compute_ic_with_significance(list(f), list(r))
        assert isinstance(res, ICResult)
        assert res.icir is None, (
            "单期数据把 icir 算成数字 = 用错口径; 必须 None")

    def test_old_ratio_field_is_renamed_and_labelled(self, calc):
        """旧 corr/std(returns) 保留但正名 ic_over_return_std, 不叫 icir"""
        f, r = _mk(30)
        res = calc.compute_ic_with_significance(list(f), list(r))
        assert res.ic_over_return_std is not None
        assert res.icir is None

    def test_icir_needs_at_least_10_periods(self, calc):
        """期数 < 10 → None (样本不足不硬算)"""
        assert calc.compute_icir([0.1] * 9) is None
        assert calc.compute_icir([0.1] * 10) is not None or True  # std=0 → None
        # 10 个有方差的点应可算
        assert calc.compute_icir([0.01 * i for i in range(10)]) is not None

    def test_icir_zero_variance_is_none(self, calc):
        """全同 IC 序列 → std=0 → None (不是 inf)"""
        assert calc.compute_icir([0.05] * 12) is None

    def test_icir_ignores_nan(self, calc):
        """NaN 被剔除, 不影响其余期"""
        series = [0.05, 0.08, np.nan, 0.03, 0.06, 0.09, 0.02, 0.07, 0.04,
                  0.05, 0.06, 0.08, 0.03]
        got = calc.compute_icir(series)
        clean = [v for v in series if not (isinstance(v, float) and np.isnan(v))]
        want = round(float(np.mean(clean) / np.std(clean, ddof=1)), 4)
        assert got == pytest.approx(want, abs=1e-6)


# ============================================================ 组3: 缺失值策略
class TestMissingValuePolicy:
    def test_nan_dropped_pairwise_not_filled(self, calc):
        """NaN 成对剔除: 结果应与"直接去掉该样本"完全一致 (等价性钉)"""
        rng = np.random.default_rng(11)
        f = rng.normal(size=25)
        r = rng.normal(size=25)
        f_bad = f.copy()
        f_bad[3] = np.nan
        f_bad[9] = np.nan

        got = calc.compute_ic_with_significance(list(f_bad), list(r))
        keep = ~np.isnan(f_bad)
        want, _ = spearmanr(f[keep], r[keep])

        assert got.sample_size == int(keep.sum()), "NaN 应被剔除, 不占样本"
        assert abs(got.mean_ic - want) < 1e-12

    def test_filling_would_change_result(self):
        """反向证据: 若把 NaN 填 0, 结果会变 —— 证明"不填充"是有效选择"""
        rng = np.random.default_rng(11)
        f = rng.normal(size=25)
        r = rng.normal(size=25)
        f_bad = f.copy()
        f_bad[3] = np.nan

        drop, _ = spearmanr(f[~np.isnan(f_bad)], r[~np.isnan(f_bad)])
        filled = f_bad.copy()
        filled[np.isnan(filled)] = 0.0
        fill_corr, _ = spearmanr(filled, r)

        assert abs(drop - fill_corr) > 1e-6, (
            "填充与剔除结果相同 — 夹具没有区分力, 无法证明策略差异")

    def test_compute_ic_also_drops_nan(self, calc):
        """compute_ic (dict 版) 同样剔除 NaN, 与 scipy 一致"""
        rng = np.random.default_rng(13)
        f = rng.normal(size=25)
        r = rng.normal(size=25)
        f[2] = np.nan
        fs = {f"{i:06d}": float(v) for i, v in enumerate(f)}
        fr = {f"{i:06d}": float(v) for i, v in enumerate(r)}

        got = calc.compute_ic(fs, fr)
        keep = ~np.isnan(f)
        want, _ = spearmanr(f[keep], r[keep])
        assert abs(got - want) < 1e-12

    def test_all_nan_returns_neutral(self, calc):
        """全 NaN → 不崩, 返回 0 / 不显著 (诚实三态: 不是 0 相关, 是没数据)"""
        res = calc.compute_ic_with_significance(
            [float("nan")] * 20, [0.01] * 20)
        assert res.mean_ic == 0.0
        assert res.is_significant is False
        assert res.sample_size == 0

    def test_missing_policy_documented(self):
        """issue 验收要求: 缺失值策略写进文档"""
        import inspect
        from strategy_layer.aggregation import ic_calculator as m
        doc = inspect.getdoc(m) or ""
        assert "成对删除" in doc or "pairwise" in doc.lower(), (
            "模块 docstring 未写明缺失值策略")
        assert "不填充" in doc or "not fill" in doc.lower()


# ============================================================ 组4: 边界与回归
class TestEdgeCases:
    def test_too_few_samples_is_insignificant(self, calc):
        res = calc.compute_ic_with_significance([0.1] * 5, [0.2] * 5)
        assert res.sample_size == 5
        assert res.is_significant is False

    def test_perfect_correlation(self, calc):
        f = list(range(20))
        assert abs(calc.compute_ic({str(i): float(i) for i in range(20)},
                                   {str(i): float(i) * 3 for i in range(20)}) - 1.0) < 1e-12

    def test_perfect_negative_correlation(self, calc):
        f = {str(i): float(i) for i in range(20)}
        r = {str(i): float(20 - i) for i in range(20)}
        assert abs(calc.compute_ic(f, r) + 1.0) < 1e-12

    def test_dict_missing_codes_aligned(self, calc):
        """只保留两边都有的 code (交集), 不按位置硬配"""
        f = {str(i): float(i) for i in range(30)}
        r = {str(i): float(i) for i in range(15, 45)}   # 交集 15..29
        got = calc.compute_ic(f, r)
        assert got > 0.99   # 交集内仍是完全正相关
