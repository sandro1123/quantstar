"""Tests for report sections (batch)"""
from unittest.mock import MagicMock
from report_layer.reports.base import ReportContext
from report_layer.sections.sector_analysis import SectorAnalysisSection
from report_layer.sections.stock_picks import StockPicksSection
from report_layer.sections.position_dynamic import PositionDynamicSection
from report_layer.sections.watch_focus import WatchFocusSection
from report_layer.sections.exit_review import ExitReviewSection
from report_layer.sections.overnight_market import OvernightMarketSection
from data_layer._models import DataSnapshot, Kline


class MockDataLayer:
    def __init__(self):
        self._kline = Kline(code="000001", date="2026-07-24", open=10, high=11,
                           low=9, close=10.5, volume=1000, amount=10000, change_pct=1.0)

    def snapshot(self, date):
        return DataSnapshot(date=date, klines=[self._kline], market=MagicMock(),
            financials=[], valuations=[], moneyflow=[], news=[], limit_up_down=[], recommendations=[], readiness=MagicMock())


class MockStrategyLayer:
    def run_daily(self, date):
        return MagicMock(signals=[MagicMock(code="000001", action="buy", strength=0.8)])

    def get_scores(self, code, date):
        return {"momentum": 0.5, "value": 0.3}


class MockExecutionLayer:
    def __init__(self):
        self.stock_pool = MagicMock()


class TestSectorAnalysis:
    def test_build_without_data(self):
        ctx = ReportContext(date="2026-07-24")
        s = SectorAnalysisSection()
        r = s.build(ctx)
        assert r is not None
        assert "板块" in r.title

    def test_build_with_data(self):
        ctx = ReportContext(date="2026-07-24", data_layer=MockDataLayer())
        s = SectorAnalysisSection()
        r = s.build(ctx)
        assert r is None or "板块" in r.title


class TestOvernightMarket:
    def test_build_returns_section(self, monkeypatch):
        monkeypatch.setattr(OvernightMarketSection, "_fetch_overnight", lambda self: {"dow": 40000.0, "nasdaq": 17000.0})
        ctx = ReportContext(date="2026-07-24")
        s = OvernightMarketSection()
        r = s.build(ctx)
        assert r is not None and "外盘" in r.title


class TestExitReview:
    def test_build_without_execution(self):
        ctx = ReportContext(date="2026-07-24")
        s = ExitReviewSection()
        r = s.build(ctx)
        assert r is None or "退出" in r.title


class TestWatchFocus:
    def test_build_without_execution(self):
        ctx = ReportContext(date="2026-07-24")
        s = WatchFocusSection()
        r = s.build(ctx)
        assert r is not None


class TestPositionDynamic:
    def test_build_without_execution(self):
        ctx = ReportContext(date="2026-07-24")
        s = PositionDynamicSection()
        r = s.build(ctx)
        assert r is None


class TestStockPicks:
    def test_build_without_strategy(self):
        ctx = ReportContext(date="2026-07-24")
        s = StockPicksSection()
        r = s.build(ctx)
        assert r is None or "精选" in r.title