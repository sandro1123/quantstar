"""P2-02 估值读侧 NULL 兼容测试 — SQL NULL → None、真实 0 保持、dividend_yield 真实读取。"""

import pytest

from common.database import Database
from data_layer._loader import DataLoader


@pytest.fixture
def db():
    database = Database(":memory:")
    database.init_db()
    conn = database.get_connection()
    for tbl in ("daily_klines", "stock_financials", "stock_valuation"):
        conn.execute(f"DROP TABLE IF EXISTS {tbl}")
    conn.execute(
        "CREATE TABLE daily_klines ("
        "code TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, "
        "volume INTEGER, amount REAL, pre_close REAL, change_pct REAL, turnover_rate REAL)"
    )
    conn.execute(
        "CREATE TABLE stock_financials ("
        "code TEXT, date TEXT, eps REAL, roe REAL, revenue REAL, profit REAL, "
        "total_assets REAL, total_liabilities REAL, net_assets REAL, "
        "operating_cash_flow REAL, gross_margin REAL, net_margin REAL)"
    )
    conn.execute(
        "CREATE TABLE stock_valuation ("
        "code TEXT, date TEXT, pe_ttm REAL, pb_mrq REAL, ps_ttm REAL, "
        "market_cap REAL, total_shares REAL, dividend_yield REAL)"
    )
    conn.execute("CREATE TABLE IF NOT EXISTS stock_info (code TEXT)")
    conn.commit()
    yield database
    database.close()


def _insert_valuation(conn, code, date, pe, pb, ps, mc, ts, dy):
    conn.execute(
        "INSERT INTO stock_valuation (code, date, pe_ttm, pb_mrq, ps_ttm, market_cap, total_shares, dividend_yield) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (code, date, pe, pb, ps, mc, ts, dy),
    )


def test_snapshot_loader_keeps_sql_null_as_none(db):
    conn = db.get_connection()
    _insert_valuation(conn, "000001", "2026-07-28", None, None, None, None, None, None)
    conn.commit()

    loader = DataLoader(db)
    vals = loader.snapshot("2026-07-28").valuations
    assert len(vals) == 1
    v = vals[0]
    assert v.pe_ttm is None
    assert v.pb_mrq is None
    assert v.ps_ttm is None
    assert v.market_cap is None
    assert v.total_shares is None
    assert v.dividend_yield is None


def test_snapshot_loader_keeps_real_zero_and_reads_dividend_yield(db):
    conn = db.get_connection()
    _insert_valuation(conn, "000001", "2026-07-28", 15.0, 0.0, 3.0, 5e8, 5e7, 0.03)
    conn.commit()

    loader = DataLoader(db)
    vals = loader.snapshot("2026-07-28").valuations
    assert len(vals) == 1
    v = vals[0]
    assert v.pe_ttm == 15.0
    assert v.pb_mrq == 0.0
    assert v.ps_ttm == 3.0
    assert v.market_cap == 5e8
    assert v.total_shares == 5e7
    assert v.dividend_yield == 0.03


def test_snapshot_loader_mixed_null_and_values(db):
    conn = db.get_connection()
    _insert_valuation(conn, "000001", "2026-07-28", None, 2.5, None, 1e9, None, None)
    conn.commit()

    loader = DataLoader(db)
    v = loader.snapshot("2026-07-28").valuations[0]
    assert v.pe_ttm is None
    assert v.pb_mrq == 2.5
    assert v.ps_ttm is None
    assert v.market_cap == 1e9
    assert v.total_shares is None
    assert v.dividend_yield is None


def test_snapshot_loader_keeps_non_numeric_raw(db):
    conn = db.get_connection()
    _insert_valuation(conn, "000001", "2026-07-28", "not-a-number", 1.0, None, None, None, None)
    conn.commit()

    loader = DataLoader(db)
    v = loader.snapshot("2026-07-28").valuations[0]
    assert v.pe_ttm == "not-a-number"
    assert v.pb_mrq == 1.0


def test_history_loader_keeps_sql_null_as_none(db):
    conn = db.get_connection()
    _insert_valuation(conn, "000001.SZ", "2025-12-16", None, None, None, None, None, None)
    conn.commit()

    loader = DataLoader(db)
    history = loader.get_history("000001.SZ", "2026-01-01", 50)
    assert len(history.valuations) == 1
    v = history.valuations[0]
    assert v.pe_ttm is None
    assert v.pb_mrq is None
    assert v.ps_ttm is None
    assert v.market_cap is None
    assert v.total_shares is None
    assert v.dividend_yield is None


def test_history_loader_reads_dividend_yield(db):
    conn = db.get_connection()
    _insert_valuation(conn, "000001.SZ", "2025-12-16", 12.0, 1.5, 2.0, 4e8, 4e7, 0.025)
    conn.commit()

    loader = DataLoader(db)
    history = loader.get_history("000001.SZ", "2026-01-01", 50)
    v = history.valuations[0]
    assert v.pe_ttm == 12.0
    assert v.dividend_yield == 0.025


def test_history_loader_partial_null_real_zero(db):
    conn = db.get_connection()
    _insert_valuation(conn, "000001.SZ", "2025-12-16", None, 0.0, None, None, None, 0.0)
    conn.commit()

    loader = DataLoader(db)
    history = loader.get_history("000001.SZ", "2026-01-01", 50)
    v = history.valuations[0]
    assert v.pe_ttm is None
    assert v.pb_mrq == 0.0
    assert v.dividend_yield == 0.0


def test_loader_survives_legacy_schema_without_dividend_yield():
    database = Database(":memory:")
    database.init_db()
    conn = database.get_connection()
    for tbl in ("daily_klines", "stock_financials", "stock_valuation"):
        conn.execute(f"DROP TABLE IF EXISTS {tbl}")
    conn.execute(
        "CREATE TABLE daily_klines ("
        "code TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, "
        "volume INTEGER, amount REAL, pre_close REAL, change_pct REAL, turnover_rate REAL)"
    )
    conn.execute(
        "CREATE TABLE stock_financials ("
        "code TEXT, date TEXT, eps REAL, roe REAL, revenue REAL, profit REAL, "
        "total_assets REAL, total_liabilities REAL, net_assets REAL, "
        "operating_cash_flow REAL, gross_margin REAL, net_margin REAL)"
    )
    conn.execute(
        "CREATE TABLE stock_valuation ("
        "code TEXT, date TEXT, pe_ttm REAL, pb_mrq REAL, ps_ttm REAL, "
        "market_cap REAL, total_shares REAL)"
    )
    conn.execute("CREATE TABLE IF NOT EXISTS stock_info (code TEXT)")
    conn.execute(
        "INSERT INTO stock_valuation (code, date, pe_ttm, pb_mrq, ps_ttm, market_cap, total_shares) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("000001", "2026-07-28", 10.0, 1.0, 2.0, 3e8, 3e7),
    )
    conn.commit()

    loader = DataLoader(database)
    v = loader.snapshot("2026-07-28").valuations[0]
    assert v.pe_ttm == 10.0
    assert v.dividend_yield is None
    database.close()
