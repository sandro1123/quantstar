"""DL-14 (Part 2): market_sentiment 连续产出 — 本地计算兜底 (源失败时)

根因: sentiment 唯一源 zzshare 不可靠 (DL-00 实测返回 0 行) → 仅 6 行产出。
修复: 源失败时从 daily_klines (涨跌家数) + stock_limit_up_down (涨跌停数)
本地计算兜底。:memory: 库, 无网络无生产库。
"""

import sqlite3

from data_layer.pipelines.base import PipelineInput
from data_layer.pipelines.sentiment import SentimentPipeline

DATE = "2026-08-15"  # 周六无数据用交易日模拟


class _FakeDB:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

    def get_connection(self):
        return self.conn


class _EmptyRegistry:
    """无任何 sentiment 源 → 触发本地兜底"""
    def get_sources_for(self, data_type):
        return []


def _mk_db():
    db = _FakeDB()
    c = db.conn
    c.execute("CREATE TABLE daily_klines (code TEXT, date TEXT, change_pct REAL)")
    c.execute("CREATE TABLE stock_limit_up_down (code TEXT, date TEXT, limit_type TEXT)")
    c.commit()
    return db


def _add_trading_day(db, date=DATE):
    c = db.conn
    # 3 涨 2 跌
    for i, pct in enumerate([5.0, 3.0, 1.0, -2.0, -4.0]):
        c.execute("INSERT INTO daily_klines VALUES (?, ?, ?)",
                  (f"{i:06d}", date, pct))
    # 2 涨停 1 跌停
    c.execute("INSERT INTO stock_limit_up_down VALUES ('000001', ?, 'limit_up')", (date,))
    c.execute("INSERT INTO stock_limit_up_down VALUES ('000002', ?, 'limit_up')", (date,))
    c.execute("INSERT INTO stock_limit_up_down VALUES ('000003', ?, 'limit_down')", (date,))
    c.commit()


def test_fallback_produces_row_when_no_source():
    """源全失败 + 本地有数据 → 兜底产出一行"""
    db = _mk_db()
    _add_trading_day(db)
    pipe = SentimentPipeline(db=db, source_registry=_EmptyRegistry())
    result = pipe.run(PipelineInput(date=DATE))
    assert result.status == "success"
    assert result.records_written == 1
    row = db.conn.execute(
        "SELECT * FROM market_sentiment WHERE date = ?", (DATE,)).fetchone()
    assert row is not None
    assert row["up_count"] == 3      # SJZT = 涨家数
    assert row["down_count"] == 2    # SJDT = 跌家数
    assert row["zt_count"] == 2      # 涨停数
    assert row["dt_count"] == 1      # 跌停数

def test_fallback_no_data_no_row():
    """非交易日/无本地数据 → 不产出空行"""
    db = _mk_db()
    pipe = SentimentPipeline(db=db, source_registry=_EmptyRegistry())
    result = pipe.run(PipelineInput(date=DATE))
    assert result.records_written == 0
    row = db.conn.execute(
        "SELECT COUNT(*) FROM market_sentiment").fetchone()[0]
    assert row == 0

def test_fallback_missing_tables_no_crash():
    """本地表不存在 → 兜底返回 None, 不崩"""
    db = _FakeDB()  # 无任何表
    pipe = SentimentPipeline(db=db, source_registry=_EmptyRegistry())
    result = pipe.run(PipelineInput(date=DATE))
    assert result.records_written == 0

def test_source_preferred_over_fallback():
    """有可用源时优先用源, 不走兜底"""
    db = _mk_db()
    _add_trading_day(db)

    class _GoodSource:
        name = "fake"
        def is_available(self):
            return True
        def get_data(self, data_type, params):
            return {"info": {"sign": "55", "ZT": 99, "DT": 1,
                             "SJZT": 3000, "SJDT": 2000}}

    class _RegWithSource:
        def get_sources_for(self, data_type):
            return [_GoodSource()]

    pipe = SentimentPipeline(db=db, source_registry=_RegWithSource())
    result = pipe.run(PipelineInput(date=DATE))
    assert result.records_written == 1
    row = db.conn.execute(
        "SELECT * FROM market_sentiment WHERE date = ?", (DATE,)).fetchone()
    assert row["zt_count"] == 99  # 来自源, 非本地兜底的 2

def test_fallback_market_temp_zero():
    """兜底 market_temp 无可靠公式, 保持 0"""
    db = _mk_db()
    _add_trading_day(db)
    pipe = SentimentPipeline(db=db, source_registry=_EmptyRegistry())
    pipe.run(PipelineInput(date=DATE))
    row = db.conn.execute(
        "SELECT market_temp FROM market_sentiment WHERE date = ?", (DATE,)).fetchone()
    assert row["market_temp"] == 0.0

def test_fallback_idempotent():
    """重跑同日 INSERT OR REPLACE 不重复"""
    db = _mk_db()
    _add_trading_day(db)
    pipe = SentimentPipeline(db=db, source_registry=_EmptyRegistry())
    pipe.run(PipelineInput(date=DATE))
    pipe.run(PipelineInput(date=DATE))
    n = db.conn.execute("SELECT COUNT(*) FROM market_sentiment").fetchone()[0]
    assert n == 1
