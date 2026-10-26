import math


def extract(klines, field):
    return [k[field] if isinstance(k, dict) else getattr(k, field) for k in klines]


def rolling_mean(values, w):
    n = len(values)
    res = [0.0] * n
    cum = 0.0
    for i in range(n):
        cum += values[i]
        if i >= w:
            cum -= values[i - w]
        if i >= w - 1:
            res[i] = cum / w
    return res


def rolling_std(values, w, means=None):
    n = len(values)
    if means is None:
        means = rolling_mean(values, w)
    res = [0.0] * n
    for i in range(w - 1, n):
        s = sum((values[j] - means[i]) ** 2 for j in range(i - w + 1, i + 1))
        res[i] = math.sqrt(s / w)
    return res


def rolling_sum(values, w):
    n = len(values)
    res = [0.0] * n
    cum = 0.0
    for i in range(n):
        cum += values[i]
        if i >= w:
            cum -= values[i - w]
        if i >= w - 1:
            res[i] = cum
    return res


def rolling_max(values, w):
    n = len(values)
    res = [0.0] * n
    for i in range(w - 1, n):
        res[i] = max(values[i - w + 1:i + 1])
    return res


def rolling_min(values, w):
    n = len(values)
    res = [0.0] * n
    for i in range(w - 1, n):
        res[i] = min(values[i - w + 1:i + 1])
    return res


def ema(values, span):
    n = len(values)
    res = [0.0] * n
    alpha = 2.0 / (span + 1)
    res[0] = values[0]
    for i in range(1, n):
        res[i] = alpha * values[i] + (1 - alpha) * res[i - 1]
    return res
