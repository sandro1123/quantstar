"""辅助算子 — auxiliary operators (scalar + vector)"""
import math

import numpy as np

# ── Scalar operators (used by expression evaluator) ──

def abs_(value: float) -> float:
    return abs(value)


def log(value: float) -> float:
    if value <= 0:
        return 0.0
    return math.log(value)


def sign(value: float) -> float:
    if value > 0:
        return 1.0
    elif value < 0:
        return -1.0
    return 0.0


def neg(value: float) -> float:
    return -value


def not_(value: float) -> float:
    return 1.0 if value == 0 else 0.0


# ── Vector operators ──

def signedpower(x: list[float], power: float = 2.0) -> list[float]:
    """带符号的幂运算: sign(x) * |x|^power"""
    arr = np.array(x)
    return (np.sign(arr) * np.abs(arr) ** power).tolist()


def abs_op(x: list[float]) -> list[float]:
    """绝对值"""
    return np.abs(x).tolist()


def log_op(x: list[float]) -> list[float]:
    """对数 (处理负数)"""
    arr = np.array(x)
    arr = np.clip(arr, 1e-10, None)
    return np.log(arr).tolist()


def sqrt_op(x: list[float]) -> list[float]:
    """平方根 (处理负数)"""
    arr = np.abs(np.array(x))
    return np.sqrt(arr).tolist()


def if_else(condition: list[bool], true_val: list[float], false_val: list[float]) -> list[float]:
    """条件选择"""
    return [t if c else f for c, t, f in zip(condition, true_val, false_val)]


# ── WorldQuant 101 Alpha 别名 ──

def stddev(values: list[float], window: int = 5) -> float:
    """Alias for ts_stddev"""
    from strategy_layer.operator_lib.ts_operators import ts_stddev
    return ts_stddev(values, window)


def delay(values: list[float], k: int = 1) -> float:
    """Alias for ts_delay"""
    from strategy_layer.operator_lib.ts_operators import ts_delay
    return ts_delay(values, k)


def correlation(values_a: list[float], values_b: list[float], window: int = 5) -> float:
    """Alias for ts_corr"""
    from strategy_layer.operator_lib.ts_operators import ts_corr
    return ts_corr(values_a, values_b, window)


def delta(values: list[float], k: int = 1) -> float:
    """Alias for ts_delta"""
    from strategy_layer.operator_lib.ts_operators import ts_delta
    return ts_delta(values, k)


def covariance(values_a: list[float], values_b: list[float], window: int = 5) -> float:
    """Alias for ts_covariance"""
    from strategy_layer.operator_lib.ts_operators import ts_covariance
    return ts_covariance(values_a, values_b, window)


def adv20(values: list[float]) -> float:
    """Average daily volume over 20 days"""
    if not values:
        return 0.0
    recent = values[-20:]
    return sum(recent) / len(recent)
