"""BT-CORRECTNESS ⑥: train / early-stop / holdout 切分带 embargo (>= 标签窗) + 逐日截面 IC。"""
from datetime import date, timedelta

import numpy as np
import pytest

from strategy_layer.ml.splits import embargoed_date_split, mean_daily_ic


def _dates(n):
    base = date(2026, 1, 1)
    return [(base + timedelta(days=i)).isoformat() for i in range(n)]


def test_embargo_gap_at_least_horizon():
    ds = _dates(300)
    sp = embargoed_date_split(ds, horizon=20, train_frac=0.7, es_frac=0.15)
    idx = {d: i for i, d in enumerate(ds)}
    tr = max(idx[d] for d in sp.train)
    es_lo, es_hi = min(idx[d] for d in sp.es_val), max(idx[d] for d in sp.es_val)
    ho = min(idx[d] for d in sp.holdout)
    assert es_lo - tr > 20
    assert ho - es_hi > 20
    assert not (sp.train & sp.es_val) and not (sp.es_val & sp.holdout) and not (sp.train & sp.holdout)
    assert len(sp.dropped) == 40


def test_short_history_gives_empty_segment():
    sp = embargoed_date_split(_dates(60), horizon=20)
    assert not sp.es_val  # 60*0.15=9 < 20 → 调用方需退化
    sp2 = embargoed_date_split(_dates(60), horizon=20, train_frac=0.8, es_frac=0.0)
    assert sp2.train and sp2.holdout
    assert min(sp2.holdout) > max(sp2.train)


def test_mean_daily_ic_cross_sectional():
    rng = np.random.default_rng(0)
    dates, pred, y = [], [], []
    for d in _dates(10):
        x = rng.normal(size=30)
        pred += list(x)
        y += list(x + 100 * len(dates))  # 截面完全单调, 但跨日水平差巨大
        dates += [d] * 30
    assert mean_daily_ic(pred, y, dates) == pytest.approx(1.0)


def test_mean_daily_ic_no_valid_day_is_nan():
    assert np.isnan(mean_daily_ic([1, 2], [1, 2], ["a", "a"]))
