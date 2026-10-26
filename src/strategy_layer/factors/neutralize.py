"""strategy_layer/factors/neutralize.py — 因子截面中性化 (卡IV后半, P1-15)

行业哑变量 + log(market_cap) OLS 残差化。教科书顺序: 去极值
(mad_clip, 在位) → 中性化 (本模块) → 标准化 (zscore, 在位)。
FactorEngine._clean 挂点: mad_clip 之后 zscore 之前。

开关 strategy.factor_neutralize.enabled 默认 False — 零现产
行为改变; 通电=纯配置翻转 (signal_provenance / #57 同先例)。

诚实语义 (与 #57 同族, 宁可降级不可假值):
- 行业/市值缺失超容忍线 (可用样本 < 半数) → 原值直通 (不可
  评估不冒充中性化);
- 单行业截面 → 行业维度无哑变量可比, 仅市值轴照常中性化;
- X 列共线 (某行业全同 cap) → lstsq 最小范数解稳, 不炸。

Löwdin 正交化: 裁量不做 (YAGNI) — 因子间正交化属聚合域,
detect_redundant_factors 相关性过滤已在位; 派单原文按可选
增强留池。
"""
import math

import numpy as np

_MIN_NEUTRAL_SAMPLES = 30  # 双侧样本下限, 少则诚实直通
_MISSING_TOLERANCE = 0.5   # 行业/市值可用样本低于半数 → 直通


def neutralize_cross_section(
    factor_name: str,
    values: list[float],
    industries: list[str | None],
    market_caps: list[float | None],
) -> list[float]:
    """OLS 残差化: y=因子值, X=[行业哑变量(drop首类)|log(cap)]。

    values/industries/market_caps 按同序对齐; 缺失 (行业空串/
    None 或 cap 非正) 的样本从回归剔除, 其残差以该股行业均值
    (或总均值) 代偿后直通 — 长度恒等于输入, 不丢股。
    """
    n = len(values)
    if n != len(industries) or n != len(market_caps):
        return list(values)  # 输入不对齐 = 不可评估, 直通
    if n < _MIN_NEUTRAL_SAMPLES:
        return list(values)

    # 可用子集: 因子有限 + 行业非空 + 市值>0
    idx_usable = [
        i for i in range(n)
        if isinstance(values[i], (int, float))
        and not isinstance(values[i], bool)
        and math.isfinite(values[i])
        and industries[i]
        and isinstance(market_caps[i], (int, float))
        and not isinstance(market_caps[i], bool)
        and market_caps[i] > 0
    ]
    if len(idx_usable) < max(_MIN_NEUTRAL_SAMPLES, int(n * _MISSING_TOLERANCE)):
        return list(values)  # 缺失超容忍线, 诚实直通

    usable_industries = sorted({industries[i] for i in idx_usable})
    if len(usable_industries) < 1:
        return list(values)

    # 行业哑变量: drop 首类防截距共线; 单行业 → 只有截距+cap
    industry_list = sorted(usable_industries)
    _industry_dummies = {ind: k for k, ind in enumerate(industry_list[1:])} \
        if len(industry_list) > 1 else {}

    rows = []
    y = []
    for i in idx_usable:
        vec = [1.0]  # 截距
        for ind in industry_list[1:]:
            vec.append(1.0 if industries[i] == ind else 0.0)
        vec.append(math.log(market_caps[i]))
        rows.append(vec)
        y.append(float(values[i]))

    X = np.asarray(rows, dtype=float)
    yv = np.asarray(y, dtype=float)
    beta, *_ = np.linalg.lstsq(X, yv, rcond=None)
    resid = yv - X @ beta

    # 代偿: 缺失样本以「同行业可用残差均值」(无则总均值) 填充
    out: list[float] = [0.0] * n
    resid_by_ind: dict[str, list[float]] = {}
    resid_all: list[float] = []
    for j, i in enumerate(idx_usable):
        r = float(resid[j])
        out[i] = r
        resid_by_ind.setdefault(industries[i], []).append(r)
        resid_all.append(r)
    global_mean = sum(resid_all) / len(resid_all)
    for i in range(n):
        if i not in idx_usable:
            peers = resid_by_ind.get(industries[i] or "")
            out[i] = (sum(peers) / len(peers)) if peers else global_mean
    return out
