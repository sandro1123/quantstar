# -*- coding: utf-8 -*-
"""p0_overview 大数字三枚 TDD (09-29 01:00 窗, Day1 mock 接线)。"""
import pandas as pd
import pytest


def _stats(exits_rows, tracking_n):
    _rets = pd.Series([r[0] for r in exits_rows if r[0] is not None])
    STATS = {"win_rate": None, "avg_pct": None, "tracking_n": tracking_n}
    if len(_rets):
        _wins = int((_rets > 0).sum())
        _losses = int((_rets < 0).sum())
        if _wins + _losses:
            STATS["win_rate"] = round(_wins / (_wins + _losses) * 100, 1)
        STATS["avg_pct"] = round(float(_rets.mean()), 2)
        STATS["exits_n"] = len(_rets)
    else:
        STATS["exits_n"] = 0
    return STATS


def test_win_rate_event_caliber():
    STATS = _stats([(5.0,), (3.0,), (2.0,), (-1.0,), (0.0,)], 23)
    assert STATS["win_rate"] == 75.0
    assert STATS["exits_n"] == 5


def test_avg_pct_not_multiplied():
    STATS = _stats([(5.01,), (1.21,)], 23)
    assert STATS["avg_pct"] == pytest.approx(3.11, abs=0.01)


def test_zero_exits_none():
    STATS = _stats([], 23)
    assert STATS["win_rate"] is None
    assert STATS["avg_pct"] is None
    assert STATS["exits_n"] == 0
    assert STATS["tracking_n"] == 23


def test_negative_avg_pct():
    STATS = _stats([(-2.0,), (-1.0,)], 23)
    assert STATS["avg_pct"] < 0
    assert STATS["win_rate"] == 0.0
