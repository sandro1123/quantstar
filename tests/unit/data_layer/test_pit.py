"""Tests for Point-in-Time data queries"""
import pytest
from common.database import Database
from data_layer.pit import PITDataProvider, PITQuery, PITResult

@pytest.fixture
def db():
    database = Database(":memory:")
    database.init_db()
    conn = database.get_connection()
    conn.execute("DROP TABLE IF EXISTS stock_financials")
    conn.execute("DROP TABLE IF EXISTS stock_valuation")
    conn.execute("DROP TABLE IF EXISTS daily_klines")
    conn.execute(
        "CREATE TABLE daily_klines ("
        "code TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, "
        "volume INTEGER, amount REAL, pre_close REAL, change_pct REAL, turnover_rate REAL)"
    )
    conn.execute(
        "CREATE TABLE stock_financials ("
        "code TEXT, date TEXT, eps REAL, roe REAL, revenue REAL, profit REAL, "
        "total_assets REAL, total_liabilities REAL, net_assets REAL, "
        "operating_cash_flow REAL, gross_margin REAL, net_margin REAL, "
        "announce_date TEXT DEFAULT '', caliber TEXT DEFAULT 'cumulative')"
    )
    conn.execute(
        "CREATE TABLE stock_valuation ("
        "code TEXT, date TEXT, pe_ttm REAL, pb_mrq REAL, ps_ttm REAL, "
        "market_cap REAL, total_shares REAL)"
    )
    conn.execute(
        "INSERT INTO daily_klines (code, date, open, close, volume) VALUES "
        "('000001.SZ', '2026-07-15', 10.0, 10.5, 100000), "
        "('000001.SZ', '2026-07-16', 10.5, 11.0, 120000), "
        "('000001.SZ', '2026-07-17', 11.0, 11.2, 90000)"
    )
    # Q1 报告期 2026-03-31, 公告日 2026-04-24; Q2 报告期 2026-06-30, 公告日 2026-08-14
    conn.execute(
        "INSERT INTO stock_financials (code, date, eps, roe, revenue, profit, announce_date) VALUES "
        "('000001.SZ', '2026-03-31', 0.50, 10.0, 1000000.0, 100000.0, '2026-04-24'), "
        "('000001.SZ', '2026-06-30', 1.20, 12.5, 2500000.0, 250000.0, '2026-08-14')"
    )
    conn.execute(
        "INSERT INTO stock_valuation (code, date, pe_ttm, pb_mrq, market_cap) VALUES "
        "('000001.SZ', '2026-07-16', 15.0, 2.0, 5000000000.0), "
        "('000001.SZ', '2026-07-17', 14.5, 1.9, 5200000000.0)"
    )
    conn.commit()
    yield database
    database.close()

def test_pit_query_exact_date_match(db):
    provider = PITDataProvider(db)
    query = PITQuery(code="000001.SZ", as_of_date="2026-07-17", table="daily_klines", columns=["close", "volume"])
    result = provider.query(query)
    assert isinstance(result, PITResult)
    assert result.code == "000001.SZ"
    assert result.data_date == "2026-07-17"
    assert result.data is not None
    assert result.data[0]["close"] == 11.2
    assert result.staleness_days == 0

def test_pit_query_fallback_to_earlier_date(db):
    provider = PITDataProvider(db)
    # Query for 2026-07-16 — the closest data on or before that date is 2026-07-16 itself
    query = PITQuery(code="000001.SZ", as_of_date="2026-07-16", table="daily_klines", columns=["open", "close"])
    result = provider.query(query)
    assert result.data_date == "2026-07-16"
    assert result.data is not None
    assert result.data[0]["open"] == 10.5
    assert result.staleness_days == 0

def test_pit_query_fallback_before_any_data(db):
    provider = PITDataProvider(db)
    # No data on or before 2026-07-14
    query = PITQuery(code="000001.SZ", as_of_date="2026-07-14", table="daily_klines", columns=["open", "close"])
    result = provider.query(query)
    assert result.data is None
    assert result.data_date is None

def test_pit_query_lookback(db):
    provider = PITDataProvider(db)
    query = PITQuery(code="000001.SZ", as_of_date="2026-07-17", table="daily_klines", lookback_days=3, columns=["close", "volume"])
    result = provider.query(query)
    assert isinstance(result.data, list)
    assert len(result.data) == 3
    assert result.data[0]["date"] == "2026-07-17"

def test_pit_query_no_data(db):
    provider = PITDataProvider(db)
    query = PITQuery(code="999999.XSHE", as_of_date="2026-07-17", table="daily_klines", columns=["close"])
    result = provider.query(query)
    assert result.data is None
    assert result.data_date is None

def test_pit_financial_query(db):
    provider = PITDataProvider(db)
    # 2026-08-20 晚于 Q2 公告日 2026-08-14 → 返回 Q2
    result = provider.query_financial("000001.SZ", "2026-08-20")
    assert result.data_date == "2026-06-30"
    assert result.data is not None
    assert result.data[0]["eps"] == 1.20

def test_pit_financial_query_before_any_report(db):
    provider = PITDataProvider(db)
    result = provider.query_financial("000001.SZ", "2025-01-01")
    assert result.data is None

def test_pit_financial_no_lookahead_before_announce(db):
    """DL-47 核心: as_of_date 在报告期之后、公告日之前 → 不得返回该期 (前视偏差)。"""
    provider = PITDataProvider(db)
    # Q2 报告期 2026-06-30, 公告日 2026-08-14。
    # 2026-07-17 时 Q2 尚未公告 → 只能看到 Q1 (公告日 2026-04-24 已过)。
    result = provider.query_financial("000001.SZ", "2026-07-17")
    assert result.data is not None
    assert result.data_date == "2026-03-31", "公告日未到的财报不得返回 (前视偏差)"
    assert result.data[0]["eps"] == 0.50

def test_pit_financial_on_announce_day_visible(db):
    """公告日当天数据即可见 (边界: announce_date <= as_of_date)。"""
    provider = PITDataProvider(db)
    result = provider.query_financial("000001.SZ", "2026-08-14")
    assert result.data_date == "2026-06-30"
    assert result.data[0]["eps"] == 1.20

def test_pit_financial_before_q1_announce(db):
    """Q1 报告期 2026-03-31 但公告日 2026-04-24 → 04-01 查询不得返回 Q1。"""
    provider = PITDataProvider(db)
    result = provider.query_financial("000001.SZ", "2026-04-01")
    assert result.data is None, "Q1 尚未公告, 应无可见财报"

def test_pit_financial_empty_announce_date_degrades(db):
    """公告日为空 (未回补) → 诚实降级为按报告期过滤 (旧行为), 不报错。"""
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO stock_financials (code, date, eps, announce_date) VALUES "
        "('600000.SH', '2026-03-31', 0.80, '')"
    )
    conn.commit()
    provider = PITDataProvider(db)
    result = provider.query_financial("600000.SH", "2026-07-01")
    assert result.data is not None
    assert result.data_date == "2026-03-31"

def test_pit_financial_staleness_days(db):
    provider = PITDataProvider(db)
    result = provider.query_financial("000001.SZ", "2026-08-20")
    # 报告期 2026-06-30 → 2026-08-20 = 51 天
    assert result.staleness_days == 51

def test_pit_valuation_query(db):
    provider = PITDataProvider(db)
    result = provider.query_valuation("000001.SZ", "2026-07-17")
    assert result.data_date == "2026-07-17"
    assert result.data is not None
    assert result.data[0]["pe_ttm"] == 14.5

def test_pit_valuation_query_fallback(db):
    provider = PITDataProvider(db)
    # No valuation data on 2026-07-15, closest is 2026-07-16
    result = provider.query_valuation("000001.SZ", "2026-07-16")
    assert result.data_date == "2026-07-16"
    assert result.data[0]["pe_ttm"] == 15.0
