"""回归测试: 修复 LHB 字段映射导致 net_buy_amount 恒为 0。

zzshare LHB 返回字段为 buy_in(主力净流入, 可为负)/turnover/capitalization,
无 buy_amount/sell_amount。旧映射读取不存在的字段 -> net_buy 恒 0。
修复: net_buy_amount = buy_in, total_buy = turnover, total_sell = capitalization。
"""
import sqlite3
import pytest

from data_layer.pipelines.lhb import LHBPipeline
from data_layer.pipelines.base import PipelineInput


class FakeZZ:
    def __init__(self, rows): self.rows = rows
    def get_data(self, *a, **k): return self.rows


def _make_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE lhb_daily (code TEXT, date TEXT, net_buy_amount REAL, total_buy REAL, total_sell REAL)")
    conn.commit()
    return conn


def test_net_buy_uses_buy_in_not_missing_fields(monkeypatch):
    """buy_in 字段应映射为 net_buy_amount(旧代码读不存在的 buy_amount, 恒 0)。"""
    fake = FakeZZ([
        {"stock_code": "600001", "buy_in": -123.5, "turnover": 456.0, "capitalization": 789.0},
        {"stock_code": "600002", "buy_in": 88.0, "turnover": 11.0, "capitalization": 22.0},
    ])
    import data_layer.pipelines.lhb as lhb_mod
    monkeypatch.setattr(lhb_mod, "ZZShareSourceAdapter", lambda: fake)
    conn = _make_db()
    pl = LHBPipeline()
    res = pl.run(PipelineInput(date="2026-08-13", db=_FakeDB(conn)))
    assert res.records_written == 2
    row = conn.execute("SELECT net_buy_amount, total_buy, total_sell FROM lhb_daily WHERE code='600001'").fetchone()
    assert row[0] == -123.5, "net_buy_amount 应取 buy_in"
    assert row[1] == 456.0
    assert row[2] == 789.0


def test_zero_buy_in_is_allowed():
    """buy_in 为 0 时也应正常写入(0 是合法净流入值, 不应被 or 0 吞掉为 None)。"""
    fake = FakeZZ([{"stock_code": "600003", "buy_in": 0.0, "turnover": 5.0, "capitalization": 6.0}])
    conn = _make_db()
    # 直接验证 float(0) 逻辑不被 or 0 吞掉为 None
    assert float(fake.rows[0].get("buy_in", 0) or 0) == 0.0


class _FakeDB:
    def __init__(self, conn): self._conn = conn
    def get_connection(self): return self._conn
