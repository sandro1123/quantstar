"""Tests for Report Layer sections"""
import pytest
from unittest.mock import MagicMock
from report_layer.reports.base import ReportContext
from report_layer.sections.base import Section
from report_layer.sections.market_overview import MarketOverviewSection
from report_layer.sections.risk_tips import RiskTipsSection
from data_layer._models import DataSnapshot, Kline


class _MockDataLayer:
    def __init__(self, klines=None):
        self._klines = klines or []

    def snapshot(self, date):
        return DataSnapshot(
            date=date,
            klines=self._klines,
            market=MagicMock(),
            financials=[],
            valuations=[],
            moneyflow=[],
            news=[],
            limit_up_down=[],
            readiness=MagicMock(),
        )


class TestMarketOverviewSection:
    def test_build_returns_none_without_data_layer(self):
        ctx = ReportContext(date="2026-07-24")
        section = MarketOverviewSection()
        result = section.build(ctx)
        assert result is None

    def test_build_returns_none_without_klines(self):
        ctx = ReportContext(date="2026-07-24", data_layer=_MockDataLayer([]))
        section = MarketOverviewSection()
        result = section.build(ctx)
        assert result is None

    def test_build_with_klines(self):
        klines = [Kline(code="000001", date="2026-07-24", open=10, high=11, low=9,
                        close=10.5, volume=1000, amount=10000, change_pct=1.5)]
        ctx = ReportContext(date="2026-07-24", data_layer=_MockDataLayer(klines))
        section = MarketOverviewSection()
        result = section.build(ctx)
        assert result is not None
        assert "盘面总览" in result.title
        assert "上涨" in result.body


class TestRiskTipsSection:
    def test_build_returns_data_without_data(self):
        ctx = ReportContext(date="2026-07-24")
        section = RiskTipsSection()
        result = section.build(ctx)
        assert result is not None
        assert "风险" in result.title

    def test_build_returns_section_with_data(self):
        klines = [Kline(code="000001", date="2026-07-24", open=10, high=11, low=9,
                        close=10.5, volume=1000, amount=10000, change_pct=10.5)]
        ctx = ReportContext(date="2026-07-24", data_layer=_MockDataLayer(klines))
        section = RiskTipsSection()
        result = section.build(ctx)
        assert result is not None


class TestSectionBase:
    def test_section_requires_name(self):
        class BadSection(Section):
            def build(self, ctx):
                return None

        with pytest.raises(TypeError):
            BadSection()