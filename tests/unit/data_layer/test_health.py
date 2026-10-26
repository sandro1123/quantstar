"""Tests for data_layer/health"""
import pytest
from common.database import Database
from data_layer.health.readiness import DataReadiness
from data_layer.health.quality import DataQuality


@pytest.fixture
def db():
    database = Database(":memory:")
    database.init_db()
    conn = database.get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_klines (
            code TEXT, date TEXT, open REAL, high REAL, low REAL,
            close REAL, volume REAL, amount REAL, pre_close REAL,
            change_pct REAL, turnover_rate REAL,
            PRIMARY KEY (code, date)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stock_info (
            code TEXT PRIMARY KEY, name TEXT NOT NULL,
            industry TEXT DEFAULT '', market TEXT DEFAULT 'SZ',
            list_date TEXT DEFAULT '', is_active INTEGER DEFAULT 1
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stock_financials (
            code TEXT, date TEXT, eps REAL, roe REAL, revenue REAL,
            profit REAL, total_assets REAL, total_liabilities REAL,
            net_assets REAL, operating_cash_flow REAL,
            gross_margin REAL, net_margin REAL,
            PRIMARY KEY (code, date)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stock_valuation (
            code TEXT, date TEXT, pe_ttm REAL, pb_mrq REAL, ps_ttm REAL,
            market_cap REAL, total_shares REAL,
            PRIMARY KEY (code, date)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS factor_daily (
            date TEXT, code TEXT, factor_scores TEXT,
            PRIMARY KEY (date, code)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stock_moneyflow (
            code TEXT, date TEXT, main_net_inflow REAL,
            PRIMARY KEY (code, date)
        )
    """)
    conn.commit()
    yield database
    database.close()


def test_readiness_check(db):
    dr = DataReadiness(db)
    report = dr.check("2026-07-17")
    assert report.date == "2026-07-17"
    assert isinstance(report.is_ready, bool)
    assert isinstance(report.tables, dict)


def test_quality_report(db):
    report = DataQuality(db).report("2026-07-17")
    assert report.date == "2026-07-17"
    assert isinstance(report.flags, list)
    assert report.is_passed is False
    assert report.metrics["daily_klines.rows"] == 0


def test_quality_report_rejects_noncanonical_date(db):
    report = DataQuality(db).report("20260717")
    assert report.is_passed is False
    assert "YYYY-MM-DD" in report.flags[0]


def test_quality_report_accepts_valid_market_rows(db):
    conn = db.get_connection()
    conn.executemany(
        "INSERT INTO stock_info (code, name, is_active) VALUES (?, ?, 1)",
        [("000001", "A"), ("000002", "B")],
    )
    conn.executemany(
        """
        INSERT INTO daily_klines
        (code, date, open, high, low, close, volume, amount)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("000001", "2026-07-17", 10, 11, 9, 10.5, 10000, 100000),
            ("000002", "2026-07-17", 20, 21, 19, 20.5, 20000, 400000),
        ],
    )
    conn.executemany(
        """
        INSERT INTO stock_valuation
        (code, date, pe_ttm, pb_mrq, ps_ttm, market_cap, total_shares)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("000001", "2026-07-17", 10, 1, 2, 1000000, 100000),
            ("000002", "2026-07-17", 20, 2, 3, 2000000, 100000),
        ],
    )
    conn.commit()

    report = DataQuality(db).report("2026-07-17")
    assert report.is_passed is True
    assert report.metrics["daily_klines.coverage_ratio"] == 1.0
    assert report.metrics["stock_valuation.coverage_ratio"] == 1.0


def test_quality_report_flags_invalid_ohlc_and_date_rows(db):
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO stock_info (code, name, is_active) VALUES ('000001', 'A', 1)"
    )
    conn.execute(
        """
        INSERT INTO daily_klines
        (code, date, open, high, low, close, volume, amount)
        VALUES ('000001', '2026-07-17', 10, 9, 11, 10, -1, -1)
        """
    )
    conn.execute(
        "INSERT INTO stock_financials (code, date) VALUES ('000001', '20260717')"
    )
    conn.commit()

    report = DataQuality(db).report("2026-07-17")
    assert report.is_passed is False
    assert report.metrics["daily_klines.invalid_ohlc_rows"] == 1
    assert report.metrics["daily_klines.invalid_trade_rows"] == 1
    assert report.metrics["stock_financials.invalid_date_rows"] == 1
