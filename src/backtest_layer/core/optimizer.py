"""组合优化 — 等权占位 / Max-Sharpe / 等权

BT-11 ③ (B-11 假货治理): 原 hrp_weights 跑完聚类后直接返回等权 (注释自认"简化"),
属假 HRP 冒充真功能。已改名 equal_weight_placeholder 明示占位语义;
真 HRP (递归二分 + 簇内方差配权) 属从零写的大工程, 按 D-18 排队。
"""


def equal_weight_placeholder(returns: dict[str, list[float]]) -> dict[str, float]:
    """等权占位 — 不是 HRP。

    BT-11 ③: 原名 hrp_weights, 实际只算了相关性/聚类就返回等权, 假实现。
    现名明示占位语义: 输入收益序列, 输出等权组合 (与 equal_weight 等价,
    仅保留收益 dict 入参形态供既有调用方过渡)。
    真 HRP 启用条件: 实现递归二分 (quasi-diagonalization) + 簇内方差配权,
    并补 HRP vs 等权的回测对比验证 — 排队中。
    """
    codes = list(returns.keys())
    if not codes:
        return {}
    n = len(codes)
    return {c: 1.0 / n for c in codes}


# 向后兼容别名 — 既有调用方/测试仍可用旧名, 但名字本身不再声称是 HRP
hrp_weights = equal_weight_placeholder


def max_sharpe_weights(returns: dict[str, list[float]], risk_free: float = 0.03) -> dict[str, float]:
    """最大夏普比率组合"""
    import numpy as np

    codes = list(returns.keys())
    if len(codes) < 2:
        return {c: 1.0 for c in codes}
    matrix = np.array([returns[c] for c in codes])
    mean_ret = np.mean(matrix, axis=1)
    cov = np.cov(matrix)
    inv_cov = np.linalg.pinv(cov)
    ones = np.ones(len(codes))
    weights = inv_cov @ (mean_ret - risk_free / 252)
    weights = weights / (ones @ weights)
    weights = np.maximum(0, weights)
    weights = weights / weights.sum()
    return {c: float(w) for c, w in zip(codes, weights)}


def equal_weight(codes: list[str]) -> dict[str, float]:
    return {c: 1.0 / len(codes) for c in codes}
