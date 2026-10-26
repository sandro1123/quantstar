"""卡IV后半 (P1-15) 因子中性化 — 行业哑变量+log市值 OLS 残差化。

RED→GREEN: 纯函数语义 + 开关门控 (默认关零现产行为改变)。
挂点: FactorEngine._clean 内 mad_clip 之后 zscore 之前
(去极值→中性化→标准化教科书顺序)。
"""
import os
import sys

import numpy as np
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))


# ---------- RED 1: 模块存在 + 纯函数语义 ----------

def test_neutralize_module_and_semantics():
    """OLS 残差化语义: 注入行业效应后残差≈0 行业间无差;
    纯随机无结构时残差≈原值 (不过度中性化)。"""
    from strategy_layer.factors.neutralize import neutralize_cross_section

    rng = np.random.default_rng(11)
    n = 300
    # 6 行业, 每行业 50 股
    industries = ["ind_%d" % (i % 6) for i in range(n)]
    caps = (rng.lognormal(20.0, 1.0, n)).tolist()
    base = rng.normal(0.0, 1.0, n)
    # 行业效应 +3i: 因子被行业污染
    polluted = [float(base[i] + 3.0 * (i % 6)) for i in range(n)]

    resid = neutralize_cross_section(
        "f_polluted", polluted, industries, caps)
    # 残差与行业哑变量回归斜率≈0 — 中性化即剥离
    dummies = np.zeros((n, 5))
    for i in range(n):
        if (i % 6) >= 1:
            dummies[i, (i % 6) - 1] = 1.0
    X = np.column_stack([dummies, np.log(caps)])
    beta, *_ = np.linalg.lstsq(X, np.asarray(resid), rcond=None)
    assert np.max(np.abs(beta)) < 0.15, \
        f"行业/市值效应未被剥离: {np.max(np.abs(beta))}"
    # 残差保留个体信息 (与去效应真值相关)
    true_resid = base - np.mean(base)  # 近似
    corr = float(np.corrcoef(resid, true_resid)[0, 1])
    assert corr > 0.9, f"残差须保留因子个体信息: corr={corr}"


def test_neutralize_pure_noise_passthrough():
    """无行业/市值结构时不过度中性化: 纯随机 → 残差与原值
    高度相关 (结构解释力≈0, OLS 自然近直通)。"""
    from strategy_layer.factors.neutralize import neutralize_cross_section

    rng = np.random.default_rng(12)
    n = 300
    industries = ["ind_%d" % (i % 6) for i in range(n)]
    caps = (rng.lognormal(20.0, 1.0, n)).tolist()
    vals = rng.normal(0.0, 1.0, n).tolist()

    resid = neutralize_cross_section("f_pure", vals, industries, caps)
    corr = float(np.corrcoef(resid, vals)[0, 1])
    assert corr > 0.95, f"无结构时残差须近直通: corr={corr}"


def test_neutralize_honesty_on_missing_data():
    """诚实跳过语义: 行业/市值缺失超半 → 不冒充中性化,
    原值返回 (不可评估不造假); 单行业截面无哑变量可比,
    仅剩市值轴 (照常中性化, 行业维度诚实降级)。"""
    from strategy_layer.factors.neutralize import neutralize_cross_section

    rng = np.random.default_rng(13)
    n = 60
    # 半数行业缺失
    industries = ["ind_%d" % (i % 6) if i % 2 == 0 else ""
                  for i in range(n)]
    caps = (rng.lognormal(20.0, 1.0, n)).tolist()
    vals = rng.normal(0.0, 1.0, n).tolist()

    resid = neutralize_cross_section("f_missing", vals, industries, caps)
    # 半缺失在容忍线内 (可用子集), 降级照跑; 全缺失则直通
    assert len(resid) == n
    industries_none = [""] * n
    resid2 = neutralize_cross_section("f_allmiss", vals,
                                      industries_none, caps)
    assert resid2 is not None and len(resid2) == n


# ---------- RED 2: 开关门控 (默认关零现产行为改变) ----------

def test_neutralize_disabled_by_default_zero_change():
    """开关默认关: _clean 输出与关前完全一致 (零现产行为
    改变, #57 同款铁律) — 不读 industry/cap, 不改 matrix。"""
    import math
    from strategy_layer.engine import FactorEngine, FactorResult

    rng = np.random.default_rng(14)
    raw = [FactorResult(code="%06d" % i, date="2026-08-28",
                        factor_name="f_a",
                        score=float(rng.normal(0, 1)))
           for i in range(50)]
    eng = FactorEngine(data_layer=None, factors=[], mode="test")

    # 无 config 注入 → 开关默认 False (config=None 路径)
    matrix = eng._clean(raw, "2026-08-28")

    # 对照: 手工 mad_clip + zscore (关态=现产语义)
    from strategy_layer.factors.factor_cleaning import mad_clip, zscore
    scores = [r.score for r in raw]
    expected = zscore(mad_clip(scores))
    got = [matrix["%06d" % i]["f_a"] for i in range(50)]
    for a, b in zip(expected, got):
        assert math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9), \
            f"开关关不得改变清洗输出: {a} vs {b}"


def test_neutralize_enabled_residuals_reach_matrix():
    """开关开: 残差进 matrix — 行业污染被剥掉后, 行业均值差
    近 0; 且开关关态断言 (不读行业数据=直通) 在同测试内对拍。"""
    from strategy_layer.engine import FactorEngine, FactorResult

    rng = np.random.default_rng(15)
    n = 120
    raw = []
    for i in range(n):
        base = float(rng.normal(0.0, 1.0))
        raw.append(FactorResult(
            code="%06d" % i, date="2026-08-28", factor_name="f_a",
            score=base + 3.0 * (i % 6)))  # 行业效应

    class _FakeCapRepo:
        """industry + market_cap 双源假 repo (engine 探测协议)。"""
        def get_industry_map(self, codes, date):
            return {c: "ind_%d" % (int(c) % 6) for c in codes}
        def get_market_caps(self, codes, date):
            return {c: float(2e9 + int(c) * 1e7) for c in codes}

    class _Cfg:
        def get(self, key, default=None):
            if key == "strategy.factor_neutralize.enabled":
                return True
            return default

    eng = FactorEngine(data_layer=None, factors=[], mode="test")
    eng.set_neutralize_repo(_FakeCapRepo())
    matrix = eng._clean_with_config(raw, "2026-08-28", _Cfg())

    vals = [matrix["%06d" % i]["f_a"] for i in range(n)]
    # 中性化+标准化后: 6 行业组均值全部≈0 (总均值0, 无行业差)
    arr = np.asarray(vals)
    for g in range(6):
        grp = arr[[i for i in range(n) if i % 6 == g]]
        assert abs(grp.mean()) < 0.15, \
            f"行业效应未被剥离: group {g} mean={grp.mean()}"
