"""午间速报 — 盘中推送 (12:00)

R3 (设计稿-C §2.4): 午报 = 候选股跟踪, 轻量一屏读完。
- 候选跟踪段置顶 (新增核心, 每候选一行=现价vs区间+推送状态)
- 市场/板块/要闻/策略居中
- 涨跌停池降附录置尾 (D4 裁定)
"""
from report_layer.reports.base import ReportBuilder
from report_layer.sections.advisory_brief import AdvisoryMiddaySection  # 卡VII: 午间投顾
from report_layer.sections.midday_alerts import MiddayAlertsSection
from report_layer.sections.midday_candidate_tracking import (
    MiddayCandidateTrackingSection,
)
from report_layer.sections.midday_market import MiddayMarketSection
from report_layer.sections.midday_news import MiddayNewsSection
from report_layer.sections.midday_sectors import MiddaySectorsSection
from report_layer.sections.midday_strategy import MiddayStrategySection

class MiddayBriefBuilder(ReportBuilder):
    @property
    def content_type(self) -> str:
        return "midday_brief"

    @property
    def title(self) -> str:
        return "☀️ QuantStar 午间速报"

    def sections(self) -> list:
        return [
            MiddayCandidateTrackingSection(),
            AdvisoryMiddaySection(),  # 卡VII: 午间投顾 (上午实时)
            MiddayMarketSection(),
            MiddaySectorsSection(),
            MiddayNewsSection(),
            MiddayStrategySection(),
            MiddayAlertsSection(),  # R3/D4: 涨跌停池降附录置尾
        ]
