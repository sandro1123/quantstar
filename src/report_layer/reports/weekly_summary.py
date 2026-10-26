"""周报 — 周末推送"""
from datetime import date as _date, timedelta as _td

from report_layer.reports.base import ReportBuilder
from report_layer.reports.evening_report import ActionSummarySection, PortfolioSection
from report_layer.sections.base import Section, SectionResult
from report_layer.sections.weekly_review import build_weekly_review


class WeeklyReviewSection(Section):
    """本周复盘段 — 接 weekly_review.build_weekly_review (P0-6/7)。

    判例「建了没接线」第八变体修 (09-29 统筹裁 a): weekly_review.py 纯函数
    建了没接 sections() — 本段把三行 (推荐数/对错/净值) 接进周报。
    诚实水位: 0 笔了结对错显式打「—」+ caveats 不凑 0 胜 0 负。
    """

    @property
    def name(self) -> str:
        return "weekly_review"

    def build(self, context) -> SectionResult | None:
        db = getattr(context.data_layer, "db", None) if context.data_layer else None
        if db is None:
            return None
        conn = db.get_connection()
        # 周界: 包含周一至周日整周。若推送日在周末或下一周，统一定位到该报告所覆盖的自然周
        d = _date.fromisoformat(str(context.date)[:10])
        monday = d - _td(days=d.weekday())
        week_start = monday.isoformat()
        week_end = (monday + _td(days=6)).isoformat()
        data = build_weekly_review(conn, week_start, week_end)
        vb = data.get("rec_by_verdict", {})
        exits_n = data.get("exits_n", 0)
        if exits_n:
            winloss = (str(data.get("exits_win", 0)) + " 胜 "
                       + str(data.get("exits_loss", 0)) + " 负"
                       + "（均笔 " + f"{data.get('exits_avg_pct', 0):+.2f}" + "%）")
        else:
            winloss = "—（本周无了结；近 30 日战绩见 dashboard P0 大数字）"
        nav = data.get("nav_week_pct")
        nav_txt = (f"{nav:+.2f}%" if nav is not None else "—")
        caveats = data.get("caveats", [])
        caveat_txt = (chr(10) + chr(10).join("· " + c for c in caveats)) if caveats else ""
        body = ("本周（" + week_start + " ~ " + week_end + "）" + chr(10)
                + "推荐 " + str(data.get("rec_total", 0)) + " 条"
                + "（买 " + str(vb.get("buy", 0)) + " · 卖 " + str(vb.get("sell", 0))
                + " · 持 " + str(vb.get("hold", 0)) + "）" + chr(10)
                + "对错  " + winloss + chr(10)
                + "净值  " + nav_txt + "（paper_trading 本周起止对比）"
                + caveat_txt)
        return SectionResult(name=self.name, title="📊 本周复盘", body=body)


class WeeklySummaryBuilder(ReportBuilder):
    @property
    def content_type(self) -> str:
        return "weekly_summary"

    @property
    def title(self) -> str:
        return "📋 QuantStar 周报"

    def sections(self) -> list:
        return [
            PortfolioSection(),
            ActionSummarySection(),
            WeeklyReviewSection(),  # P0-6/7 接线 (判例「建了没接线」第八变体修)
        ]
