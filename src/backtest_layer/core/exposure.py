"""因子暴露分析 — Fama-French 风格归因

BT-11 ⑥ (B-11 排队态标注): style_exposure 当前零生产消费者 (仅单测覆盖),
按 D-18 标排队态, 保留代码不删。factor_exposure 同属排队 (无生产调用方)。
启用条件: ① 有真实因子收益序列供给 (依赖 DL-23 index_daily / 因子收益基建);
② 回测报告或周报链路消费暴露归因结果; ③ 补生产调用方单测。
"""


def factor_exposure(returns: list[float], market_returns: list[float] | None = None) -> dict:
    if len(returns) < 2:
        return {"alpha": 0.0, "beta": 0.0, "r_squared": 0.0}
    n = len(returns)
    if market_returns is None or len(market_returns) != n:
        market_returns = [0.0] * n
    mean_r = sum(returns) / n
    mean_m = sum(market_returns) / n
    cov = sum((returns[i] - mean_r) * (market_returns[i] - mean_m) for i in range(n))
    var_m = sum((m - mean_m) ** 2 for m in market_returns)
    beta = cov / var_m if var_m > 0 else 0.0
    alpha = mean_r - beta * mean_m
    ss_res = sum((returns[i] - (alpha + beta * market_returns[i])) ** 2 for i in range(n))
    ss_tot = sum((r - mean_r) ** 2 for r in returns)
    r_squared = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return {"alpha": round(alpha, 4), "beta": round(beta, 4), "r_squared": round(r_squared, 4)}


def style_exposure(returns: list[float], factors: dict[str, list[float]]) -> dict[str, float]:
    """多因子暴露分析（简化版：相关系数）"""
    n = len(returns)
    if n < 2:
        return {}
    exposures = {}
    for name, factor_returns in factors.items():
        if len(factor_returns) != n:
            continue
        mean_r = sum(returns) / n
        mean_f = sum(factor_returns) / n
        num = sum((returns[i] - mean_r) * (factor_returns[i] - mean_f) for i in range(n))
        den = (sum((r - mean_r) ** 2 for r in returns) * sum((f - mean_f) ** 2 for f in factor_returns)) ** 0.5
        exposures[name] = round(num / den, 4) if den > 0 else 0.0
    return exposures
