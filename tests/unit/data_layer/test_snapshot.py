"""Tests for data_layer snapshot"""
import pytest
from common.database import Database
from data_layer._models import DataSnapshot, HistoryWindow, IntradaySnapshot
from data_layer._loader import DataLoader


@pytest.fixture
def db():
    database = Database(":memory:")
    database.init_db()
    conn = database.get_connection()
    # Drop and recreate with full schemas for testing range queries
    conn.execute("DROP TABLE IF EXISTS daily_klines")
    conn.execute("DROP TABLE IF EXISTS stock_financials")
    conn.execute("DROP TABLE IF EXISTS stock_valuation")
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
    conn.commit()
    yield database
    database.close()


def test_snapshot_returns_dataclass(db):
    loader = DataLoader(db)
    snapshot = loader.snapshot("2026-07-17")
    assert isinstance(snapshot, DataSnapshot)
    assert snapshot.date == "2026-07-17"


def test_snapshot_intraday_returns_dataclass(db):
    loader = DataLoader(db)
    snapshot = loader.snapshot_intraday()
    assert isinstance(snapshot, IntradaySnapshot)
    assert snapshot.timestamp is not None


def test_get_history_returns_history_window(db):
    loader = DataLoader(db)
    history = loader.get_history("000001.SZ", "2026-01-01", 50)
    assert isinstance(history, HistoryWindow)
    assert history.code == "000001.SZ"
    assert history.end_date == "2026-01-01"
    assert history.window == 50
    assert history.klines == []
    assert history.financials == []
    assert history.valuations == []
    assert history.moneyflow == []


def test_get_history_with_data(db):
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO daily_klines (code, date, open, high, low, close, volume, amount, pre_close, change_pct, turnover_rate) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("000001.SZ", "2025-12-15", 10.0, 11.0, 9.5, 10.5, 100000, 1050000, 10.2, 2.94, 1.5),
    )
    conn.execute(
        "INSERT INTO daily_klines (code, date, open, high, low, close, volume, amount, pre_close, change_pct, turnover_rate) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("000001.SZ", "2025-12-16", 10.5, 11.2, 10.3, 11.0, 120000, 1320000, 10.5, 4.76, 1.8),
    )
    conn.execute(
        "INSERT INTO stock_financials (code, date, eps, roe, revenue, profit, total_assets, total_liabilities, net_assets, operating_cash_flow, gross_margin, net_margin) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("000001.SZ", "2025-12-01", 1.5, 12.0, 5000000, 800000, 10000000, 3000000, 7000000, 200000, 25.0, 16.0),
    )
    conn.execute(
        "INSERT INTO stock_valuation (code, date, pe_ttm, pb_mrq, ps_ttm, market_cap, total_shares) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("000001.SZ", "2025-12-16", 15.0, 2.5, 3.0, 500000000, 50000000),
    )
    conn.commit()

    loader = DataLoader(db)
    history = loader.get_history("000001.SZ", "2026-01-01", 50)

    assert len(history.klines) == 2
    assert history.klines[0].code == "000001.SZ"
    assert history.klines[0].open == 10.0
    assert history.klines[1].close == 11.0
    assert len(history.financials) == 1
    assert history.financials[0].eps == 1.5
    assert len(history.valuations) == 1
    assert history.valuations[0].pe_ttm == 15.0


def test_snapshot_range_returns_list(db):
    loader = DataLoader(db)
    snapshots = loader.snapshot_range("2026-01-01", "2026-01-03")
    assert isinstance(snapshots, list)
    assert len(snapshots) == 3
    assert all(isinstance(s, DataSnapshot) for s in snapshots)
    assert snapshots[0].date == "2026-01-01"
    assert snapshots[2].date == "2026-01-03"


def test_snapshot_range_single_day(db):
    loader = DataLoader(db)
    snapshots = loader.snapshot_range("2026-07-17", "2026-07-17")
    assert len(snapshots) == 1
    assert snapshots[0].date == "2026-07-17"


def test_snapshot_range_empty(db):
    loader = DataLoader(db)
    snapshots = loader.snapshot_range("2026-01-01", "2026-01-03")
    assert len(snapshots) == 3
    for s in snapshots:
        assert s.klines == []


def test_get_history_moneyflow_missing_table(db):
    loader = DataLoader(db)
    history = loader.get_history("000001.SZ", "2026-01-01", 10)
    assert history.moneyflow == []