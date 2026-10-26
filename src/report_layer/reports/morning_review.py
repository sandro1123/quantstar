"""开盘速报 — 09:35 推送"""
from report_layer.reports.base import ReportBuilder
from report_layer.sections.market_overview import MarketOverviewSection
from report_layer.sections.sector_analysis import SectorAnalysisSection
from report_layer.sections.stock_picks import StockPicksSection
from report_layer.sections.watch_focus import WatchFocusSection


class MorningReviewBuilder(ReportBuilder):
    @property
    def content_type(self) -> str:
        return "morning_review"

    @property
    def title(self) -> str:
        return "🚀 QuantStar 开盘速报"

    def sections(self) -> list:
        return [
            MarketOverviewSection(),
            SectorAnalysisSection(),
            StockPicksSection(),
            WatchFocusSection(),
        ]
