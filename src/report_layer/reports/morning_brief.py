"""早报 — 开盘前推送 (08:00)

R1 (2026-08-28): 行动指引化重编排 (报告体系重设计稿-C §2.2)。
QS = 股票推荐系统非交易系统 (F-20260828 定位校准): 指引段前置、
背景段后置、系统日志段移出晨报。七段结构:

    1. outbox_watermark    投递水位 (基建置顶, 无滞留自动隐身)
    2. action_candidates   🎯 今日行动候选 (行动卡组, D-277 七要素)
    3. avoid_cards         🔴 回避卡 (ST 不进买入位)
    4. yesterday_followup  🧵 昨日候选, 现在如何 (连续性段)
    5. market_background   📊 市场背景 (外盘+指数+要闻压缩)
    6. risk_compliance     ⚠️ 风险提示 + 合规固定段
    7. conclusion          结论 (大盘研判 + 仓位建议)

移出晨报 (旧模块保留, 供其他报告/CLI 复用):
    overnight_market/news/indices → 并入 market_background
    watch_focus/stock_picks       → 由 action_candidates 取代
    ic_weight                     → D2 移出 (数据留 marimo P 页)
    market_overview               → 并入背景 (日志段)
    agent_trade_plan              → D5 移出 (待并入结论, R3)
"""
from report_layer.reports.base import ReportBuilder
from report_layer.sections.advisory_brief import AdvisoryMorningSection  # 卡VII: 晨间投顾
from report_layer.sections.action_candidates import ActionCandidatesSection
from report_layer.sections.avoid_cards import AvoidCardsSection
from report_layer.sections.conclusion import ConclusionSection
from report_layer.sections.market_background import MarketBackgroundSection
from report_layer.sections.outbox_watermark import OutboxWatermarkSection
from report_layer.sections.risk_compliance import RiskComplianceSection
from report_layer.sections.yesterday_followup import YesterdayFollowupSection


class MorningBriefBuilder(ReportBuilder):
    @property
    def content_type(self) -> str:
        return "morning_brief"

    @property
    def title(self) -> str:
        return "🌅 QuantStar 早报"

    def sections(self) -> list:
        return [
            OutboxWatermarkSection(),   # 基建置顶, 无滞留自动隐身
            ActionCandidatesSection(),  # 🎯 今日行动候选
            AvoidCardsSection(),        # 🔴 回避卡
            AdvisoryMorningSection(),   # 🧭 晨间投顾 (隔夜+昨收视角)
            YesterdayFollowupSection(), # 🧵 昨日候选, 现在如何
            MarketBackgroundSection(),  # 📊 市场背景 (压缩段)
            RiskComplianceSection(),    # ⚠️ 风险提示 + 合规固定段
            ConclusionSection(),        # 结论
        ]
