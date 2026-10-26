"""BT-CORRECTNESS ⑦: 公告日为空的财报行按法定披露截止日判定可得 (不再放行 = 前视)。"""
import sqlite3
from datetime import date

import pytest

from data_layer.pit import (
    PITDataProvider,
    financial_available_date,
    statutory_disclosure_deadline,
)


def _d(y, m, d):
    return date(y, m, d).isoformat()


@pytest.mark.parametrize("period,deadline", [
    (_d(2026, 3, 31), _d(2026, 4, 30)),
    (_d(2026, 6, 30), _d(2026, 8, 31)),
    (_d(2026, 9, 30), _d(2026, 10, 31)),
    (_d(2025, 12, 31), _d(2026, 4, 30)),
    (_d(2026, 6, 30).replace("-", ""), _d(2026, 8, 31)),   # YYYYMMDD
    (_d(2026, 9, 30)[:7].replace("-", ""), _d(2026, 10, 31)),  # YYYYMM
])
def test_statutory_deadline(period, deadline):
    assert statutory_disclosure_deadline(period) == deadline


def test_available_date_prefers_announced():
    assert financial_available_date(_d(2026, 6, 30), _d(2026, 8, 14), None) == _d(2026, 8, 14)
    assert financial_available_date(_d(2026, 6, 30), None, _d(2026, 8, 20)) == _d(2026, 8, 20)
    assert financial_available_date(_d(2026, 6, 30), "", "") == _d(2026, 8, 31)
    assert statutory_disclosure_deadline(None) is None


class _Db:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("CREATE TABLE stock_financials (code TEXT, date TEXT, eps REAL, "
                          "announce_date TEXT DEFAULT '')")

    def get_connection(self):
        return self.conn


def test_query_financial_null_announce_hidden_until_deadline():
    db = _Db()
    db.conn.executemany("INSERT INTO stock_financials VALUES (?,?,?,?)", [
        ("600000", _d(2026, 3, 31), 0.5, ""),
        ("600000", _d(2026, 6, 30), 0.9, None),
    ])
    p = PITDataProvider(db)
    # 7/15: H1 未公告且未到 8/31 截止日 → 只能看到 Q1 (旧实现返回 H1 = 前视)
    assert p.query_financial("600000", _d(2026, 7, 15)).data_date == _d(2026, 3, 31)
    assert p.query_financial("600000", _d(2026, 8, 31)).data_date == _d(2026, 6, 30)
    # 4/15: Q1 未到截止日 → 无可见财报
    assert p.query_financial("600000", _d(2026, 4, 15)).data is None


def test_query_financial_real_announce_date_used():
    db = _Db()
    db.conn.execute("INSERT INTO stock_financials VALUES (?,?,?,?)",
                    ("600000", _d(2026, 6, 30), 0.9, _d(2026, 7, 10)))
    p = PITDataProvider(db)
    assert p.query_financial("600000", _d(2026, 7, 10)).data_date == _d(2026, 6, 30)
    assert p.query_financial("600000", _d(2026, 7, 9)).data is None
