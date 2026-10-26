# -*- coding: utf-8 -*-
"""tushare per-stock unit regression: _get_klines must apply vol*100 / amount*1000 (bulk parity).

Bulk path get_daily_market already converts; _get_klines missed it (live landmine
300379/300391 383 rows). Uses stub pro + stub frame rows, no network.
"""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from data_layer.sources.tushare_source import TushareSourceAdapter


class _Row(dict):
    def get(self, k, d=None):
        return super().get(k, d)


class _DF:
    empty = False

    def __init__(self, rows):
        self._rows = [_Row(r) for r in rows]

    def __len__(self):
        return len(self._rows)

    def iterrows(self):
        return enumerate(self._rows)


class _Pro:
    def __init__(self, rows):
        self._rows = rows

    def daily(self, **kw):
        return _DF(self._rows)


def _adapter():
    a = TushareSourceAdapter.__new__(TushareSourceAdapter)
    return a


def test_get_klines_applies_unit_conversion():
    a = _adapter()
    pro = _Pro([{
        "close": 10.0, "pre_close": 9.9, "open": 9.9, "high": 10.1, "low": 9.8,
        "vol": 100.0, "amount": 50.0, "trade_date": "20260115",
    }])
    out = a._get_klines(pro, {"code": "600000", "start_date": "2026-01-15", "end_date": "2026-01-15"})
    assert len(out) == 1
    k = out[0]
    assert k.volume == 10000.0, k.volume
    assert k.amount == 50000.0, k.amount
    assert k.date == "2026-01-15"
    # k judge must land in clean bucket: (50000/10000)/10 = 0.5
    assert 0.5 <= (k.amount / k.volume) / k.close < 2


def test_get_klines_empty_df_returns_empty():
    a = _adapter()

    class _Empty:
        empty = True

    class _ProE:
        def daily(self, **kw):
            return _Empty()

    assert a._get_klines(_ProE(), {"code": "600000"}) == []
