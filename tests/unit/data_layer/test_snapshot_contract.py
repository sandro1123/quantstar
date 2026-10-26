"""DL-15: snapshot() 契约修复 — 补齐 moneyflow/news/limit_up_down 真实查询,
移除 holdings/recommendations (用户预决策 A+B)。

:memory: 库构造有数据日期, 断言三字段非空; 断言 DataSnapshot 无 holdings/recommendations。
"""

import sqlite3
from dataclasses import fields

from data_layer._loader import DataLoader
from data_layer._models import DataSnapshot

DATE = "2026-08-18"


class _FakeDB:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

    def get_connection(self):
        return self.conn


def _mk_db_with_data():
    db = _FakeDB()
    c = db.conn
    # DataReadiness.REQUIRED_TABLES 须存在 (snapshot 调 readiness.check)
    c.execute("CREATE TABLE daily_klines (code TEXT, date TEXT, open REAL, "
              "high REAL, low REAL, close REAL, volume REAL, amount REAL, "
              "pre_close REAL, change_pct REAL, PRIMARY KEY (code, date))")
    c.execute("CREATE TABLE stock_info (code TEXT PRIMARY KEY, name TEXT, "
              "is_active INTEGER DEFAULT 1)")
    c.execute("CREATE TABLE stock_financials (code TEXT, date TEXT)")
    c.execute("CREATE TABLE stock_valuation (code TEXT, date TEXT)")
    # 三个待补齐表
    c.execute("CREATE TABLE stock_moneyflow (code TEXT, date TEXT, "
              "main_net_inflow REAL, retail_net_inflow REAL, "
              "main_inflow REAL, main_outflow REAL)")
    c.execute("CREATE TABLE stock_news (code TEXT, date TEXT, title TEXT, "
              "source TEXT, sentiment TEXT, sentiment_score REAL, summary TEXT)")
    c.execute("CREATE TABLE stock_limit_up_down (code TEXT, date TEXT, name TEXT, "
              "limit_type TEXT, price REAL, change_pct REAL, count INTEGER)")
    # 插入当日数据
    c.execute("INSERT INTO daily_klines VALUES ('000001', ?, 10,11,9,10.5,1000,10000,10,5.0)", (DATE,))
    c.execute("INSERT INTO stock_info VALUES ('000001', '平安银行', 1)")
    c.execute("INSERT INTO stock_moneyflow VALUES ('000001', ?, 1e6, -2e5, 5e6, 4e6)", (DATE,))
    c.execute("INSERT INTO stock_news VALUES ('000001', ? || ' 10:00:00', '测试新闻', 'em', 'positive', 70.0, '摘要')", (DATE,))
    c.execute("INSERT INTO stock_limit_up_down VALUES ('002820', ?, '桂发祥', '涨停', 11.15, 9.96, 2)", (DATE,))
    c.commit()
    return db


def test_snapshot_moneyflow_populated():
    db = _mk_db_with_data()
    loader = DataLoader(db)
    snap = loader.snapshot(DATE)
    assert len(snap.moneyflow) == 1
    assert snap.moneyflow[0].code == "000001"
    assert snap.moneyflow[0].main_net_inflow == 1e6

def test_snapshot_news_populated():
    db = _mk_db_with_data()
    loader = DataLoader(db)
    snap = loader.snapshot(DATE)
    assert len(snap.news) == 1
    assert snap.news[0].title == "测试新闻"
    assert snap.news[0].sentiment_label == "positive"
    assert snap.news[0].stock_code == "000001"

def test_snapshot_limit_up_down_populated():
    db = _mk_db_with_data()
    loader = DataLoader(db)
    snap = loader.snapshot(DATE)
    assert len(snap.limit_up_down) == 1
    assert snap.limit_up_down[0].code == "002820"
    assert snap.limit_up_down[0].limit_type == "涨停"
    assert snap.limit_up_down[0].涨停次数 == 2

def test_snapshot_empty_date_returns_empty_lists():
    """无数据日期 → 三字段为空列表 (不抛错)"""
    db = _mk_db_with_data()
    loader = DataLoader(db)
    snap = loader.snapshot("2020-01-01")
    assert snap.moneyflow == []
    assert snap.news == []
    assert snap.limit_up_down == []

def test_snapshot_missing_tables_returns_empty():
    """三表不存在 → 空列表不抛错 (各 _get_ 有 try/except)"""
    db = _FakeDB()
    c = db.conn
    c.execute("CREATE TABLE daily_klines (code TEXT, date TEXT, open REAL, "
              "high REAL, low REAL, close REAL, volume REAL, amount REAL, "
              "pre_close REAL, change_pct REAL)")
    c.execute("CREATE TABLE stock_info (code TEXT PRIMARY KEY, name TEXT)")
    c.execute("CREATE TABLE stock_financials (code TEXT, date TEXT)")
    c.execute("CREATE TABLE stock_valuation (code TEXT, date TEXT)")
    c.commit()
    loader = DataLoader(db)
    snap = loader.snapshot(DATE)
    assert snap.moneyflow == []
    assert snap.news == []
    assert snap.limit_up_down == []

def test_datasnapshot_no_holdings_recommendations():
    """DL-15: DataSnapshot 移除 holdings/recommendations (执行层概念, 单向依赖)"""
    field_names = {f.name for f in fields(DataSnapshot)}
    assert "holdings" not in field_names
    assert "recommendations" not in field_names

def test_datasnapshot_has_three_fields():
    field_names = {f.name for f in fields(DataSnapshot)}
    assert "moneyflow" in field_names
    assert "news" in field_names
    assert "limit_up_down" in field_names

def test_moneyflow_date_format_compat():
    """stock_moneyflow date 可能为 YYYYMMDD (无连字符), 两种格式都查"""
    db = _mk_db_with_data()
    db.conn.execute(
        "INSERT INTO stock_moneyflow VALUES ('600519', '20260818', 2e6, 0, 0, 0)")
    db.conn.commit()
    loader = DataLoader(db)
    snap = loader.snapshot(DATE)
    codes = {m.code for m in snap.moneyflow}
    assert "600519" in codes  # YYYYMMDD 格式也被查到
