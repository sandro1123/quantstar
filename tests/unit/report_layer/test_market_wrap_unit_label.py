# -*- coding: utf-8 -*-
"""R-K6: market_wrap 量能单位标签 — volume 是股(成交量),/1e6=百万股,禁印"百万手"(错100×)。

修法依据 memory qs-rk6-market-wrap-volume-unit-20261011: 标签一行
(百万手→百万股), 必须与 eq 修复同批或在其后 — 现 eq 已并 main, 故可改。
手法: 真渲染 MarketWrapSection.build(context) 一遍, 断 body 签名;
llm_service=None → llm_call 回 None 走规则降级, 无外部依赖, 不 mock build 本身。
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from report_layer.sections.market_wrap import MarketWrapSection


class _DB:
    def __init__(self, conn):
        self._conn = conn

    def get_connection(self):
        return self._conn


class _DL:
    def __init__(self, db):
        self.db = db


class _Ctx:
    date = "2026-09-29"
    data_layer = None
    strategy_layer = object()
    llm_service = None
    agent_layer = None


def _ctx_with_rows(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE daily_klines (code TEXT, date TEXT, change_pct REAL, volume REAL)")
    conn.executemany("INSERT INTO daily_klines VALUES (?,?,?,?)", rows)
    conn.commit()
    ctx = _Ctx()
    ctx.data_layer = _DL(_DB(conn))
    return ctx


def test_unit_label_is_million_shares_not_lots():
    # 3 行 avg volume = 16_000_000 股 → 16.0 百万股; 若仍标"百万手"则错 100×
    ctx = _ctx_with_rows([
        ("600000", "2026-09-29", 1.0, 16000000.0),
        ("000001", "2026-09-29", -1.0, 16000000.0),
        ("300001", "2026-09-29", 0.5, 16000000.0),
    ])
    res = MarketWrapSection().build(ctx)
    assert res is not None
    assert "16.0" in res.body
    assert "百万股" in res.body
    assert "百万手" not in res.body


def test_empty_day_still_degrades_honestly():
    ctx = _ctx_with_rows([])
    res = MarketWrapSection().build(ctx)
    assert res is not None
    assert "积累中" in res.body
