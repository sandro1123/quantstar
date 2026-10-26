"""DL-17: is_suspended 判据修正 — 真停牌/新股/数据缺失三场景 (F-12)

旧判据"无 kline 行 = 停牌"误判新股与数据缺失; 新判据组合
is_active + 当日kline缺失 + 市场当日有行情。:memory: 库, 无生产依赖。
"""

import sqlite3

from unittest.mock import patch

from data_layer._loader import DataLoader

DATE = "2026-08-18"


class _FakeDB:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

    def get_connection(self):
        return self.conn


def _mk_db():
    db = _FakeDB()
    c = db.conn
    c.execute("CREATE TABLE daily_klines (code TEXT, date TEXT, close REAL, "
              "PRIMARY KEY (code, date))")
    c.execute("CREATE TABLE stock_info (code TEXT PRIMARY KEY, name TEXT, "
              "list_date TEXT, is_active INTEGER DEFAULT 1, delist_date TEXT)")
    c.commit()
    return db


def _add_stock(db, code, list_date="20100101", is_active=1, delist_date=""):
    db.conn.execute(
        "INSERT INTO stock_info VALUES (?, ?, ?, ?, ?)",
        (code, f"股{code}", list_date, is_active, delist_date))
    db.conn.commit()


def _add_kline(db, code, date=DATE):
    db.conn.execute(
        "INSERT INTO daily_klines VALUES (?, ?, 10.0)", (code, date))
    db.conn.commit()


# --- 场景 1: 真停牌 (在市 + 市场开市 + 该股缺 kline) ---

def test_true_suspension():
    db = _mk_db()
    _add_stock(db, "000001")
    _add_kline(db, "600519")  # 市场当日有行情 (他股有 kline)
    loader = DataLoader(db)
    assert loader.is_suspended("000001", DATE) is True

def test_not_suspended_when_has_kline():
    db = _mk_db()
    _add_stock(db, "000001")
    _add_kline(db, "000001")
    loader = DataLoader(db)
    assert loader.is_suspended("000001", DATE) is False


# --- 场景 2: 新股 (list_date 晚于当日 → 非停牌) ---

def test_new_stock_not_suspended():
    db = _mk_db()
    _add_stock(db, "301999", list_date="20260901")  # 当日尚未上市
    _add_kline(db, "600519")  # 市场有行情
    loader = DataLoader(db)
    assert loader.is_suspended("301999", DATE) is False

def test_new_stock_list_date_hyphen_format():
    """list_date 可能为 YYYY-MM-DD, 归一化比较"""
    db = _mk_db()
    _add_stock(db, "301999", list_date="2026-09-01")
    _add_kline(db, "600519")
    loader = DataLoader(db)
    assert loader.is_suspended("301999", DATE) is False


# --- 场景 3: 数据缺失 (市场当日无行情 → 不判停牌) ---

def test_market_no_data_not_suspended():
    """市场当日无 kline (非交易日/数据缺失) → 无法区分, 不判停牌"""
    db = _mk_db()
    _add_stock(db, "000001")
    # 不给任何当日 kline
    loader = DataLoader(db)
    assert loader.is_suspended("000001", DATE) is False


# --- 边界: 退市/不在市/未知股 ---

def test_delisted_not_suspended():
    db = _mk_db()
    _add_stock(db, "000002", delist_date="20260101")  # 当日已退市
    _add_kline(db, "600519")
    loader = DataLoader(db)
    assert loader.is_suspended("000002", DATE) is False

def test_inactive_not_suspended():
    db = _mk_db()
    _add_stock(db, "000003", is_active=0)
    _add_kline(db, "600519")
    loader = DataLoader(db)
    assert loader.is_suspended("000003", DATE) is False

def test_unknown_stock_not_suspended():
    db = _mk_db()
    _add_kline(db, "600519")
    loader = DataLoader(db)
    assert loader.is_suspended("999999", DATE) is False

def test_missing_tables_returns_false():
    """表不存在 → False 不抛错"""
    db = _FakeDB()
    loader = DataLoader(db)
    assert loader.is_suspended("000001", DATE) is False

import pytest


@pytest.fixture(autouse=True)
def _no_baostock():
    """DL-44: 旧测试专测推断判据, 屏蔽 baostock 适配器"""
    with patch.object(DataLoader, "_get_baostock_adapter", return_value=None):
        yield
