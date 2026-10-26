"""Config + Data 集成测试"""
import os
import pytest
from common.database import Database
from config_layer import get_config, reset_config
from data_layer import DataLayer


def test_config_data_integration():
    reset_config()
    config = get_config("dev")
    config.load()
    assert config.get_profile() == "dev"

    db = Database(":memory:")
    db.init_db()
    conn = db.get_connection()
    for table, cols in [
        ("daily_klines", "code TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, amount REAL, pre_close REAL, change_pct REAL, turnover_rate REAL"),
        ("stock_info", "code TEXT PRIMARY KEY, name TEXT"),
        ("stock_financials", "code TEXT, date TEXT, eps REAL, roe REAL, revenue REAL, profit REAL, total_assets REAL, total_liabilities REAL, net_assets REAL, operating_cash_flow REAL, gross_margin REAL, net_margin REAL"),
        ("stock_valuation", "code TEXT, date TEXT, pe_ttm REAL, pb_mrq REAL, ps_ttm REAL, market_cap REAL, total_shares REAL"),
    ]:
        conn.execute(f"CREATE TABLE IF NOT EXISTS {table} ({cols})")
    conn.execute("INSERT INTO daily_klines (code, date, open, high, low, close, volume, amount, pre_close, change_pct) VALUES ('000001', '2026-07-17', 10, 11, 9, 10.5, 1000000, 10000000, 10, 0.05)")
    conn.commit()
    dl = DataLayer(db, config)
    snapshot = dl.snapshot("2026-07-17")
    assert snapshot is not None
    assert snapshot.date == "2026-07-17"
    dl.shutdown()
    db.close()


def test_config_register_from_data_layer():
    reset_config()
    config = get_config("dev")
    config.register("data.test_key", str, "test_value")
    assert config.get("data.test_key") == "test_value"


def test_bootstrap_creates_app():
    from bootstrap import bootstrap_app
    app = bootstrap_app("dev")
    assert app.config is not None
    assert app.data is not None
    assert app.strategy is not None
    app.shutdown()