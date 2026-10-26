"""截面算子 — cross-sectional operators (scalar + vector)"""
import numpy as np
from scipy.stats import rankdata

# ── Scalar operators (used by expression evaluator) ──

def rank(values: list[float]) -> float:
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    rank_map = {v: i / max(len(sorted_vals) - 1, 1) for i, v in enumerate(sorted_vals)}
    return rank_map.get(values[-1], 0.5)


def scale(values: list[float], target: float = 1.0) -> float:
    if not values:
        return 0.0
    last = values[-1]
    total = sum(abs(v) for v in values)
    if total == 0:
        return 0.0
    return last / total * target


def clip(value: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def demean(values: list[float]) -> float:
    if not values:
        return 0.0
    mean = sum(values) / len(values)
    return values[-1] - mean


# ── Vector operators (cross-sectional, group-aware) ──

def group_rank(values: list[float], groups: list[str] | None = None) -> list[float]:
    """截面排名 (0~1)"""
    arr = np.array(values)
    if groups is None:
        ranked = rankdata(arr) / len(arr)
        return ranked.tolist()
    result = np.zeros_like(arr)
    for g in set(groups):
        mask = np.array([grp == g for grp in groups])
        if mask.sum() > 1:
            result[mask] = rankdata(arr[mask]) / mask.sum()
    return result.tolist()


def group_scale(values: list[float], groups: list[str] | None = None) -> list[float]:
    """截面标准化 (均值为0, 标准差为1)"""
    arr = np.array(values, dtype=np.float64)
    if groups is None:
        return ((arr - np.mean(arr)) / np.std(arr)).tolist() if np.std(arr) > 0 else np.zeros_like(arr).tolist()
    result = np.zeros_like(arr)
    for g in set(groups):
        mask = np.array([grp == g for grp in groups])
        if mask.sum() > 1:
            sub = arr[mask]
            result[mask] = (sub - np.mean(sub)) / np.std(sub) if np.std(sub) > 0 else np.zeros_like(sub)
    return result.tolist()


def group_zscore(values: list[float], groups: list[str] | None = None) -> list[float]:
    """Alias for group_scale"""
    return group_scale(values, groups)


def group_sum(values: list[float], groups: list[str]) -> list[float]:
    """组内求和"""
    arr = np.array(values, dtype=np.float64)
    result = np.zeros_like(arr)
    for g in set(groups):
        mask = np.array([grp == g for grp in groups])
        result[mask] = np.sum(arr[mask])
    return result.tolist()


def group_mean(values: list[float], groups: list[str]) -> list[float]:
    """组内均值"""
    arr = np.array(values, dtype=np.float64)
    result = np.zeros_like(arr)
    for g in set(groups):
        mask = np.array([grp == g for grp in groups])
        result[mask] = np.mean(arr[mask])
    return result.tolist()
