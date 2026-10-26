"""strategy_layer/ml/splits.py — 时间序列切分 (embargo) + 截面 IC

BT-CORRECTNESS ⑥ (2026-10): model_retrain 原 80/20 按日期切分, 标签为 20 日前向收益,
切分点前最后 20 个交易日的标签窗与验证期重叠 (泄漏); 且验证集同时用作 early stopping
并作为 val_ic 汇报 (乐观偏差)。本模块提供:
  - embargoed_date_split: train / early-stop-val / holdout 三段, 每个边界前丢弃
    >= horizon 个交易日 (embargo >= 标签窗)
  - mean_daily_ic: 逐日截面 Spearman 的均值 (而非混池 Spearman)
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class DateSplit:
    train: set = field(default_factory=set)
    es_val: set = field(default_factory=set)
    holdout: set = field(default_factory=set)
    embargo_days: int = 0
    dropped: set = field(default_factory=set)


def embargoed_date_split(dates, horizon: int, train_frac: float = 0.7,
                         es_frac: float = 0.15) -> DateSplit:
    """按唯一日期升序切三段, 每段边界之前丢弃 horizon 个日期 (embargo)。

    dates: 可迭代的交易日 (样本日期, 即标签起点); horizon: 标签前向交易日数。
    train = [0, a - h), es_val = [a, b - h), holdout = [b, n) 其中 a = n*train_frac,
    b = n*(train_frac+es_frac)。这样 train 的标签窗 (t, t+h] 不触及 es_val/holdout,
    es_val 的标签窗不触及 holdout。日期不足时某段可能为空 — 调用方需判空。
    """
    uniq = sorted({d for d in dates if d})
    n = len(uniq)
    h = max(int(horizon), 0)
    a = int(n * train_frac)
    b = int(n * (train_frac + es_frac))
    train = set(uniq[:max(a - h, 0)])
    es_val = set(uniq[a:max(b - h, a)])
    holdout = set(uniq[b:])
    dropped = set(uniq) - train - es_val - holdout
    return DateSplit(train=train, es_val=es_val, holdout=holdout, embargo_days=h, dropped=dropped)


def mean_daily_ic(pred, y, dates, min_names: int = 5) -> float:
    """逐日截面 Spearman IC 的均值; 截面样本 < min_names 的日期跳过。无可用日期 → nan。"""
    from scipy.stats import spearmanr

    groups: dict = defaultdict(lambda: ([], []))
    for p, t, d in zip(pred, y, dates):
        groups[d][0].append(float(p))
        groups[d][1].append(float(t))
    ics = []
    for ps, ts in groups.values():
        if len(ps) < min_names or len(set(ps)) < 2 or len(set(ts)) < 2:
            continue
        ic = spearmanr(ps, ts)[0]
        if ic == ic:  # not nan
            ics.append(ic)
    if not ics:
        return float("nan")
    return float(sum(ics) / len(ics))
