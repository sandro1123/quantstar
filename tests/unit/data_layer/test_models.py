"""Tests for data_layer/_models.py"""
import pytest
from data_layer._models import (
    DataSnapshot, IntradaySnapshot, Kline, Financial, Valuation,
    Moneyflow, NewsItem, LimitUpDown, MarketState, ReadinessReport,
    TableStatus, RealTimeQuote, MinuteBar, Anomaly, HotStock,
)


def test_kline_dataclass():
    k = Kline(code="000001", date="2026-07-17", open=10.0, high=10.5,
              low=9.8, close=10.2, volume=1000000, amount=10200000)
    assert k.code == "000001"
    assert k.close == 10.2


def test_financial_dataclass():
    f = Financial(code="000001", date="2026-07-17", eps=0.85, roe=15.3)
    assert f.eps == 0.85


def test_valuation_dataclass():
    v = Valuation(code="000001", date="2026-07-17", pe_ttm=8.5, pb_mrq=1.2)
    assert v.pe_ttm == 8.5
    assert v.ps_ttm is None
    assert v.market_cap is None
    assert v.total_shares is None
    assert v.dividend_yield is None


def test_valuation_dataclass_defaults_none():
    v = Valuation(code="000001", date="2026-07-17")
    assert v.pe_ttm is None
    assert v.pb_mrq is None
    assert v.ps_ttm is None
    assert v.market_cap is None
    assert v.total_shares is None
    assert v.dividend_yield is None


def test_market_state():
    m = MarketState(state="正常", indices=[], market_temperature=65.0,
                    up_count=2500, down_count=1500)
    assert m.state == "正常"
    assert m.market_temperature == 65.0


def test_data_snapshot():
    snapshot = DataSnapshot(
        date="2026-07-17",
        market=MarketState("正常", [], 65.0, 2500, 1500),
        klines=[], financials=[], valuations=[],
        moneyflow=[], news=[], limit_up_down=[],
        readiness=ReadinessReport(date="2026-07-17", is_ready=True, tables={}),
    )
    assert snapshot.date == "2026-07-17"


def test_intraday_snapshot_all_optional():
    snap = IntradaySnapshot(timestamp="20260717 14:30:00")
    assert snap.realtime_quotes is None
    assert snap.anomalies is None


def test_history_window():
    from data_layer._models import HistoryWindow

    window = HistoryWindow(
        code="000001.SZ",
        end_date="2026-01-01",
        window=50,
        klines=[],
        financials=[],
        valuations=[],
    )
    assert window.code == "000001.SZ"
    assert window.window == 50
    assert len(window.klines) == 0


def test_history_window_defaults():
    from data_layer._models import HistoryWindow

    window = HistoryWindow(code="000001.SZ", end_date="2026-01-01", window=20)
    assert window.klines == []
    assert window.financials == []
    assert window.valuations == []
    assert window.moneyflow == []