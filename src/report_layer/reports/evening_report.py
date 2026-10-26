"""晚报 — 盘后报告

R3 (设计稿-C §2.5): 晚报 = 今日候选复盘, 按生命线组织。
新编排:
    报告宪法 v2 (2026-09-10 第一波): 旗舰结构置顶, 旧段类保留供测试 import。
    可读性施工 (2026-09-20): 大盘信息只出现 1 次 (market_wrap);
    行动类段 (明日推荐) 靠前。
    结构重构 (TASK_20260922_01 Phase B · 批复裁决1/2):
    持仓管家段退役 — 持有组改为三行式监控 (覆盖行/待行动行/一致行),
    由 evening_storyline 持有中组承担 (持仓信息仍全卡只此 1 次);
    明日推荐收敛 Top3 + 折叠计数 (宁缺毋滥); 附录 skip_entry 同因聚合一行。
    编排如下:
    0. 🔁 昨日推荐点评 (yesterday_picks_review, 信誉位打头, 宪法 #2)
    1. 📌 明日推荐 (tomorrow_picks, Top3+折叠, 行动类靠前, 宪法 #1)
    2. 📊 今日大盘 (market_wrap, 宪法 #4 — 大盘信息只此 1 次)
    3. 📡 板块分析 + 热点新闻 (sector_news)
    4. 🗓 明日预演 + 系统健康 (preview_health)
    5. 🧵 今日候选故事线 (evening_storyline, 按动作分组聚合摘要,
       持有中组 = 三行式持续监控 — 持仓信息只此 1 次)
    6. 📋 退出归因卡 (exit_attribution, 只列有实质复盘者)
    7. 附录: 今日系统动作 (action_summary, 同因合并计数, 降级次要位)
    8. 🌙 三段式收尾 (evening_close, 明日方向=次日连续性钩子)
    9. ⚠️ 风险提示 + 合规固定段 (risk_compliance, 范式 8)

旧 MarketSummarySection / ExitQualitySection / PositionDynamicsSection /
PortfolioSection / MarketOneLinerSection 类保留 (供既有测试 import),
仅移出编排 (市场一句话与 market_wrap 重复)。
position_steward.py 模块不动 (供外部 import), 仅移出晚报编排
(TASK_20260922_01 Phase B · 批复裁决2: 持有组改三行式监控入故事线)。
"""

from report_layer.reports.base import ReportBuilder, ReportContext
from report_layer.sections.base import Section, SectionResult
from report_layer.sections.evening_close import EveningCloseSection
from report_layer.sections.evening_storyline import EveningStorylineSection
from report_layer.sections.exit_attribution import ExitAttributionSection
from report_layer.sections.risk_compliance import RiskComplianceSection
from report_layer.sections.market_wrap import MarketWrapSection
from report_layer.sections.preview_health import PreviewHealthSection
from report_layer.sections.premarket_review import PremarketReviewSection  # noqa: F401 (盘前速报复用)
from report_layer.sections.sector_news import SectorNewsSection
from report_layer.sections.tomorrow_picks import TomorrowPicksSection
from report_layer.sections.yesterday_picks_review import YesterdayPicksReviewSection

import re

class MarketSummarySection(Section):
    """R3: 移出晚报编排 (纯日志段, 被 market_one_liner 替换); 类保留。"""
    @property
    def name(self) -> str:
        return "market_summary"

    def build(self, ctx: ReportContext) -> SectionResult | None:
        if not ctx.data_layer:
            return None
        snap = ctx.data_layer.snapshot(ctx.date)
        return SectionResult(
            name=self.name,
            title="盘面总结",
            body=(
                f"日期: {ctx.date}\n"
                f"K 线数据: {len(snap.klines)} 只股票\n"
                f"估值数据: {len(snap.valuations)} 只\n"
                f"财务数据: {len(snap.financials)} 只\n"
            ),
        )

class ActionSummarySection(Section):
    """R3/D4 同精神: 系统动作明细降级附录 (事后佐证次要位)。

    TASK_20260922_01 Phase B: skip_entry 等系统动作按 (类型, 归一化原因)
    合并计数一行 — 不再逐只罗列, 不带原始百分比/阈值参数 (深钻页给证据)。
    """
    @property
    def name(self) -> str:
        return "action_summary"

    _ACTION_LABEL = {"skip_entry": "风控拦截"}

    @classmethod
    def _short_reason(cls, reason: str, drop_intercept: bool) -> str:
        """归一化原因: 冒号分段, 含数字/代码的参数段整段去掉; skip_entry 的
        「风控闸门拦截」头段与类型标签重复, 一并去掉。分组键也用此值。"""
        parts = [p.strip() for p in re.split(r"[:：]", reason or "")]
        keep = [p for p in parts
                if p and not re.search(r"\d", p)
                and not (drop_intercept and "拦截" in p)]
        return "，".join(keep) if keep else "（原因未细分）"

    def build(self, ctx: ReportContext) -> SectionResult | None:
        if not ctx.execution_layer:
            return None
        feed = ctx.execution_layer.get_today_actions()
        actions = feed.actions if hasattr(feed, "actions") else (feed or [])
        if not actions:
            return SectionResult(name=self.name, title="附录：今日系统动作",
                                 body="今日无系统动作")

        def _g(o, k, default=None):
            if isinstance(o, dict):
                return o.get(k, default)
            return getattr(o, k, default)

        groups: dict = {}
        for a in actions:
            atype = _g(a, "action_type", "") or ""
            short = self._short_reason(_g(a, "reason", "") or "",
                                       drop_intercept=(atype == "skip_entry"))
            key = (atype, short)
            groups[key] = groups.get(key, 0) + 1
        lines = []
        for (atype, short), n in sorted(groups.items(),
                                        key=lambda kv: (-kv[1], kv[0])):
            label = self._ACTION_LABEL.get(atype, atype or "系统动作")
            unit = "只" if atype == "skip_entry" else "次"
            lines.append(f"{label} {n} {unit}（原因：{short}）")
        return SectionResult(name=self.name, title="附录：今日系统动作",
                             body="\n".join(lines))

class PortfolioSection(Section):
    @property
    def name(self) -> str:
        return "portfolio"

    def build(self, ctx: ReportContext) -> SectionResult | None:
        if not ctx.execution_layer:
            return None
        snap = ctx.execution_layer.get_portfolio_snapshot()
        return SectionResult(
            name=self.name,
            title="持仓概览",
            body=(
                f"持有中: {snap.active} 只\n"
                f"待入场: {snap.pending_entry} 只\n"
                f"待退出: {snap.pending_exit} 只\n"
                f"观察中: {snap.watch} 只\n"
                f"验证组合净值: {snap.portfolio_nav:.2f}\n"
                f"今日盈亏: {snap.today_pnl:+.2f}\n"
            ),
        )

class PositionDynamicsSection(Section):
    """R3: 持仓盯市改句式 (范式 1 句式宪法): 「你的持仓 X 现 +3.3%，距参考卖出线 2.1%」。

    措辞表: 止损线→参考卖出线; 净值→验证组合净值。
    """
    @property
    def name(self) -> str:
        return "position_dynamics"

    def build(self, ctx: ReportContext) -> SectionResult | None:
        if not ctx.execution_layer:
            return None
        lines = []
        snapshot = ctx.execution_layer.get_portfolio_snapshot()
        for pos in snapshot.positions:
            def _g(k, default=None):
                if hasattr(pos, "__getitem__"):
                    return pos.get(k, default)
                return getattr(pos, k, default)
            code = _g("code", "")
            name = _g("name", "") or code
            pnl = _g("pnl_pct", 0) or 0
            stop = _g("stop_loss", 0) or 0
            last = _g("last_price", 0) or 0
            label = f"{name}({code})" if name != code else code
            stop_str = ""
            if stop > 0 and last > 0:
                dist = (last - stop) / last * 100
                stop_str = f"，距参考卖出线 {dist:.1f}%"
            lines.append(f"你的持仓 {label} 现 {pnl:+.1f}%{stop_str}")
        return SectionResult(name=self.name, title="持仓盯市",
                             body="\n".join(lines) if lines else "今日无持仓")

class ExitQualitySection(Section):
    """R3: 移出晚报编排 (被 exit_attribution 升级替换); 类保留。"""
    @property
    def name(self) -> str:
        return "exit_quality"

    def build(self, ctx: ReportContext) -> SectionResult | None:
        if not ctx.execution_layer:
            return None
        lines = []
        reviews = ctx.execution_layer.get_exit_reviews(limit=5)
        for r in reviews:
            post = r.post_exit_t if hasattr(r, 'post_exit_t') else []
            # F5.5: post_exit_tracking 真实列名 check_point / price_change_since_exit
            post_str = ", ".join(
                f"{p.get('check_point', '') if hasattr(p, 'get') else getattr(p, 'check_point', '')}"
                f": {(p.get('price_change_since_exit', 0) or 0) if hasattr(p, 'get') else (getattr(p, 'price_change_since_exit', 0) or 0):+.1%}"
                for p in post[:3] if (p.get('price_change_since_exit') if hasattr(p, 'get') else getattr(p, 'price_change_since_exit', None)) is not None
            )
            if hasattr(r, "code"):
                code = r.code
                name = r.name if hasattr(r, "name") else ""
                ret = r.total_return_pct if hasattr(r, "total_return_pct") else 0
                days = r.holding_days if hasattr(r, "holding_days") else 0
            else:
                code = r.get('code', '')
                name = r.get('name', '')
                ret = r.get('total_return_pct', 0)
                days = r.get('holding_days', 0)
            lines.append(f"{code} {name}: 收益 {ret:+.1f}%, 持有 {days}天" + (f" | {post_str}" if post_str else ""))
        return SectionResult(name=self.name, title="退出跟踪", body="\n".join(lines) if lines else "暂无退出记录")

class EveningReportBuilder(ReportBuilder):
    @property
    def content_type(self) -> str:
        return "evening_report"

    @property
    def title(self) -> str:
        return "📊 QuantStar 晚报"

    def sections(self) -> list:
        # 报告宪法 v2 第一波 (2026-09-10): 昨日推荐点评信誉位打头。
        # 可读性施工 (2026-09-20): 大盘 2 段→1 段 (market_wrap),
        # 行动类段 (明日推荐) 靠前; 移出的段类保留在上方供既有测试 import。
        # 结构重构 (TASK_20260922_01 Phase B · 批复裁决2):
        # 持仓管家段移出编排 — 持有组改三行式监控, 由 evening_storyline 承担
        # (持仓信息全卡仍只出现 1 次)。
        return [
            YesterdayPicksReviewSection(),  # 🔁 昨日推荐点评 (宪法 #2 置顶)
            TomorrowPicksSection(),         # 📌 明日推荐 (Top3+折叠, 行动类靠前)
            MarketWrapSection(),            # 📊 今日大盘总结 (宪法 #4)
            SectorNewsSection(),            # 📡 板块分析 + 热点新闻
            PreviewHealthSection(),         # 🗓 明日预演 + 系统健康
            EveningStorylineSection(),      # 🧵 今日候选故事线 (含持有三行式)
            ExitAttributionSection(),       # 📋 退出归因卡 (只列有实质复盘者)
            ActionSummarySection(),         # 附录: 今日系统动作 (同因聚合)
            EveningCloseSection(),          # 🌙 三段式收尾
            RiskComplianceSection(),        # ⚠️ 风险提示 + 合规固定段
        ]
