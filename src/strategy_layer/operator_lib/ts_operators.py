"""时间序列算子 — ts_sum, ts_rank, ts_stddev, ts_corr, ts_covariance, ts_zscore, ts_min, ts_max, ts_argmax, ts_argmin, ts_skew, ts_kurt, ts_decaylinear, ts_wma, ts_delay, ts_delta"""
import math

# 退化方差守卫 (刀4案2⑦ 双侧修复, 统筹特批): 常数/近常数序列 (A股一字板)
# 的浮点舍入使 var 不为精确 0, 原 `== 0` 守卫漏触发, 进入 0/0 未定义区
# 输出 O(1) 垃圾值。1e-12 远小于任何合法价格序列的方差 (价格尺度 ~1e-4,
# 收益率尺度 ~1e-6), 不会误伤 — 安全性论证见 docs/alpha-vec-tolerance.md。
# vec_ops.py 从本模块导入 _VAR_EPS, 两侧同源。
_VAR_EPS = 1e-12
_STD_EPS = _VAR_EPS ** 0.5  # 1e-6, std 侧等价守卫


def ts_sum(values: list[float], window: int = 5) -> float:
    if not values or window <= 0:
        return 0.0
    return sum(values[-window:])


def ts_rank(values: list[float], window: int = 5) -> float:
    if not values or window <= 0:
        return 0.0
    recent = values[-window:]
    sorted_vals = sorted(recent)
    rank_map = {v: i / max(len(sorted_vals) - 1, 1) for i, v in enumerate(sorted_vals)}
    return rank_map.get(recent[-1], 0.5)


def ts_stddev(values: list[float], window: int = 5) -> float:
    if not values or window <= 1:
        return 0.0
    recent = values[-window:]
    mean = sum(recent) / len(recent)
    variance = sum((x - mean) ** 2 for x in recent) / len(recent)
    return math.sqrt(variance)


def ts_corr(values_a: list[float], values_b: list[float], window: int = 5) -> float:
    if len(values_a) < window or len(values_b) < window:
        return 0.0
    a = values_a[-window:]
    b = values_b[-window:]
    n = len(a)
    mean_a = sum(a) / n
    mean_b = sum(b) / n
    cov = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b))
    var_a = sum((x - mean_a) ** 2 for x in a)
    var_b = sum((y - mean_b) ** 2 for y in b)
    if var_a < _VAR_EPS or var_b < _VAR_EPS:
        return 0.0
    return cov / math.sqrt(var_a * var_b)


def ts_covariance(values_a: list[float], values_b: list[float], window: int = 5) -> float:
    if len(values_a) < window or len(values_b) < window:
        return 0.0
    a = values_a[-window:]
    b = values_b[-window:]
    n = len(a)
    mean_a = sum(a) / n
    mean_b = sum(b) / n
    return sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b)) / n


def ts_zscore(values: list[float], window: int = 5) -> float:
    if not values or window <= 1:
        return 0.0
    recent = values[-window:]
    mean = sum(recent) / len(recent)
    std = ts_stddev(values, window)
    if std < _STD_EPS:
        return 0.0
    return (recent[-1] - mean) / std


def ts_min(values: list[float], window: int = 5) -> float:
    if not values or window <= 0:
        return 0.0
    return float(min(values[-window:]))


def ts_max(values: list[float], window: int = 5) -> float:
    if not values or window <= 0:
        return 0.0
    return float(max(values[-window:]))


def ts_argmax(values: list[float], window: int = 5) -> int:
    if not values or window <= 0:
        return -1
    recent = values[-window:]
    return int(recent.index(max(recent)))


def ts_argmin(values: list[float], window: int = 5) -> int:
    if not values or window <= 0:
        return -1
    recent = values[-window:]
    return int(recent.index(min(recent)))


def ts_skew(values: list[float], window: int = 5) -> float:
    if not values or window < 3:
        return 0.0
    recent = values[-window:]
    n = len(recent)
    mean = sum(recent) / n
    std = ts_stddev(values, window)
    if std < _STD_EPS:
        return 0.0
    return sum((x - mean) ** 3 for x in recent) / (n * std ** 3)


def ts_kurt(values: list[float], window: int = 5) -> float:
    if not values or window < 4:
        return 0.0
    recent = values[-window:]
    n = len(recent)
    mean = sum(recent) / n
    std = ts_stddev(values, window)
    if std < _STD_EPS:
        return 0.0
    return sum((x - mean) ** 4 for x in recent) / (n * std ** 4) - 3.0


def ts_decaylinear(values: list[float], window: int = 5) -> float:
    if not values or window <= 0:
        return 0.0
    recent = values[-window:]
    weights = list(range(1, len(recent) + 1))
    total_weight = sum(weights)
    return sum(w * v for w, v in zip(weights, recent)) / total_weight


def ts_wma(values: list[float], window: int = 5) -> float:
    return ts_decaylinear(values, window)


def ts_delay(values: list[float], d: int = 1) -> float:
    if not values or d <= 0:
        return values[-1] if values else 0.0
    if d >= len(values):
        return 0.0
    return values[-(d + 1)]


def ts_delta(values: list[float], d: int = 1) -> float:
    if not values or d <= 0:
        return 0.0
    if d >= len(values):
        return 0.0
    return values[-1] - values[-(d + 1)]
