"""Factor cleaning: MAD outlier removal, neutralization, z-score standardization"""
import math
from statistics import median


def mad_clip(values: list[float], n_mad: float = 5.0) -> list[float]:
    if not values:
        return values

    clean = [v for v in values if not math.isnan(v)]
    if not clean:
        return [0.0] * len(values)

    med = median(clean)
    mad = median([abs(v - med) for v in clean]) or 1.0
    return [max(med - n_mad * mad, min(med + n_mad * mad, v)) if not math.isnan(v) else 0.0 for v in values]

def zscore(values: list[float]) -> list[float]:
    if not values:
        return values

    clean = [v for v in values if not math.isnan(v)]
    if not clean:
        return [0.0] * len(values)

    mean = sum(clean) / len(clean)
    n = len(clean)
    std = math.sqrt(sum((v - mean) ** 2 for v in clean) / max(1, n - 1)) if n > 1 else 1.0
    return [(v - mean) / std if not math.isnan(v) else 0.0 for v in values] if std > 0 else [0.0] * len(values)
