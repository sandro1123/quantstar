"""Tests for forward_return

DL-24 语义: 兜底从 daily_klines 现算"该日后第 N 个交易日收盘", 返回百分点
(与 factor_daily 存储单位一致); 后续交易日不足 horizon → None, 不造假 0。
"""
import pytest
from common.database import Database
from data_layer._loader import DataLoader


@pytest.fixture
def db():
    database = Database(":memory:")
    database.init_db()
    conn = database.get_connection()
    conn.execute("DROP TABLE IF EXISTS factor_daily")
    conn.execute("DROP TABLE IF EXISTS daily_klines")
    conn.execute(
        "CREATE TABLE factor_daily ("
        "code TEXT, date TEXT, fwd_return_1d REAL, fwd_return_5d REAL, fwd_return_20d REAL)"
    )
    conn.execute(
        "CREATE TABLE daily_klines ("
        "code TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, "
        "volume INTEGER, amount REAL, pre_close REAL, change_pct REAL, turnover_rate REAL)"
    )
    conn.commit()
    yield database
    database.close()


def _add_klines(conn, code, closes, start="2026-01-05"):
    """按序插入连续交易日 kline (行序即交易日序)"""
    from datetime import datetime, timedelta
    d = datetime.strptime(start, "%Y-%m-%d")
    for close in closes:
        conn.execute(
            "INSERT INTO daily_klines (code, date, close) VALUES (?, ?, ?)",
            (code, d.strftime("%Y-%m-%d"), close),
        )
        d += timedelta(days=1)
    conn.commit()


def test_forward_return_from_factor_daily(db):
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO factor_daily (code, date, fwd_return_5d) VALUES (?, ?, ?)",
        ("000001.SZ", "2026-01-05", 0.035),
    )
    conn.commit()
    loader = DataLoader(db)
    result = loader.forward_return("000001.SZ", "2026-01-05", 5)
    assert result == 0.035


def test_forward_return_fallback_from_klines(db):
    """DL-24: 兜底现算 = 该日后第 N 个交易日收盘, 返回百分点"""
    conn = db.get_connection()
    # 6 个交易日: 01-05 close 10.0, 第 5 个交易日后 (01-10) close 10.5 → +5%
    _add_klines(conn, "000001.SZ", [10.0, 10.1, 10.2, 10.3, 10.4, 10.5])
    loader = DataLoader(db)
    result = loader.forward_return("000001.SZ", "2026-01-05", 5)
    assert result == pytest.approx(5.0)


def test_forward_return_fallback_insufficient_future_returns_none(db):
    """DL-24: 后续交易日不足 horizon → None (旧实现此处造假 0/假值)"""
    conn = db.get_connection()
    _add_klines(conn, "000001.SZ", [10.0, 10.5])  # 仅 1 个后续交易日
    loader = DataLoader(db)
    assert loader.forward_return("000001.SZ", "2026-01-05", 5) is None
    # 最新日期 (无任何后续) → None
    assert loader.forward_return("000001.SZ", "2026-01-06", 5) is None


def test_forward_return_no_data_returns_none(db):
    loader = DataLoader(db)
    result = loader.forward_return("000001.SZ", "2026-01-05", 5)
    assert result is None


def test_forward_return_unmapped_horizon_fallback(db):
    """horizon 不在 factor_daily 列映射中 → 仍从 kline 现算 (百分点)"""
    conn = db.get_connection()
    # 11 个交易日: 01-05 close 10.0, 第 10 个交易日后 close 11.0 → +10%
    _add_klines(conn, "000001.SZ",
                [10.0, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8, 10.9, 11.0])
    loader = DataLoader(db)
    result = loader.forward_return("000001.SZ", "2026-01-05", 10)
    assert result == pytest.approx(10.0)


def test_forward_return_1d_from_factor_daily(db):
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO factor_daily (code, date, fwd_return_1d) VALUES (?, ?, ?)",
        ("000001.SZ", "2026-01-05", -0.01),
    )
    conn.commit()
    loader = DataLoader(db)
    result = loader.forward_return("000001.SZ", "2026-01-05", 1)
    assert result == -0.01


def test_forward_return_20d_from_factor_daily(db):
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO factor_daily (code, date, fwd_return_20d) VALUES (?, ?, ?)",
        ("000001.SZ", "2026-01-05", 0.12),
    )
    conn.commit()
    loader = DataLoader(db)
    result = loader.forward_return("000001.SZ", "2026-01-05", 20)
    assert result == 0.12
