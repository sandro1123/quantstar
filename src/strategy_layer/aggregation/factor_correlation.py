"""因子相关性分析 — 两两 Spearman Rank Correlation，冗余因子标记"""
import numpy as np
from scipy.stats import spearmanr

from common.logging import get_logger

logger = get_logger("strategy.factor_correlation")

CORRELATION_THRESHOLD = 0.7


def compute_factor_correlations(factor_scores: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
    """计算因子两两 Spearman 相关系数矩阵

    Args:
        factor_scores: {factor_name: {code: score}}
    Returns:
        {factor_name: {other_factor: correlation}}
    """
    factor_names = list(factor_scores.keys())
    n = len(factor_names)
    if n < 2:
        return {}

    # 构建股票代码并集
    all_codes = set()
    for scores in factor_scores.values():
        all_codes.update(scores.keys())
    codes = sorted(all_codes)

    # 构建矩阵: (n_factors, n_stocks)
    matrix = np.zeros((n, len(codes)))
    for i, fname in enumerate(factor_names):
        for j, code in enumerate(codes):
            matrix[i, j] = factor_scores[fname].get(code, 0.0)

    # 计算相关系数
    corr_matrix = {}
    for i in range(n):
        corr_matrix[factor_names[i]] = {}
        for j in range(n):
            if i == j:
                continue
            if np.std(matrix[i]) > 0 and np.std(matrix[j]) > 0:
                corr, _ = spearmanr(matrix[i], matrix[j])
                corr_matrix[factor_names[i]][factor_names[j]] = round(float(corr), 4)
            else:
                corr_matrix[factor_names[i]][factor_names[j]] = 0.0

    return corr_matrix


def detect_redundant_factors(correlations: dict[str, dict[str, float]],
                               threshold: float = CORRELATION_THRESHOLD) -> set[str]:
    """检测冗余因子（相关性 > threshold 的因子对）

    Returns:
        set of redundant factor names (相关性高的因子对中，保留一个，标记另一个)
    """
    redundant = set()
    processed = set()

    for f1 in sorted(correlations.keys()):
        if f1 in processed:
            continue
        for f2 in sorted(correlations[f1].keys()):
            if f2 in processed:
                continue
            corr = abs(correlations[f1].get(f2, 0))
            if corr > threshold:
                redundant.add(f2)
                processed.add(f2)
        processed.add(f1)

    if redundant:
        logger.info(f"冗余因子检测: {len(redundant)} 个因子相关性 > {threshold}: {sorted(redundant)}")
    return redundant


def get_effective_weights(weights: dict[str, float],
                           redundant_factors: set[str]) -> dict[str, float]:
    """获取有效权重（冗余因子权重减半）

    Args:
        weights: 原始权重 {factor_name: weight}
        redundant_factors: 冗余因子集合
    Returns:
        调整后的权重
    """
    adjusted = {}
    total_penalty = 0.0
    for f, w in weights.items():
        if f in redundant_factors:
            adjusted[f] = w * 0.5
            total_penalty += w * 0.5
        else:
            adjusted[f] = w

    # 将惩罚的权重重新分配给非冗余因子
    if total_penalty > 0 and adjusted:
        non_redundant = {f: w for f, w in adjusted.items() if f not in redundant_factors}
        non_redundant_total = sum(non_redundant.values())
        if non_redundant_total > 0:
            for f in adjusted:
                if f not in redundant_factors:
                    adjusted[f] += total_penalty * (adjusted[f] / non_redundant_total)

    return adjusted
