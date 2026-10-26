"""盘中异动告警"""
from datetime import datetime

from report_layer.reports.base import ReportBuilder, ReportContext
from report_layer.sections.base import SectionResult


class IntradayAlertSection:
    name = "intraday_alert"

    def build(self, ctx: ReportContext) -> SectionResult | None:
        if not ctx.execution_layer:
            return None
        feed = ctx.execution_layer.get_today_actions()
        raw_actions = feed.actions if hasattr(feed, "actions") else (feed or [])
        def _g(o, k, default=None):
            if isinstance(o, dict):
                return o.get(k, default)
            return getattr(o, k, default)
        alerts = [a for a in raw_actions if _g(a, "action_type") in ("exit_check", "pnl_change")]
        if not alerts:
            now = datetime.now().strftime("%H:%M")
            return SectionResult(name=self.name, title="盘中异动", body=f"截至 {now} 今日无盘中异动")
        lines = [f"今日异动 ({len(alerts)} 条):"]
        for a in alerts[:10]:
            t = _g(a, "created_at", "")
            c = _g(a, "code", "")
            typ = _g(a, "action_type", "")
            reason = _g(a, "reason", "")
            pnl = _g(a, "pnl_pct", "")
            pnl_str = f" 盈亏: {pnl:+.2f}%" if pnl else ""
            lines.append(f"  {t} {c} [{typ}] {reason}{pnl_str}")
        return SectionResult(name=self.name, title="盘中异动", body="\n".join(lines))


class PriceAlertSection:
    name = "price_alert"

    def build(self, ctx: ReportContext) -> SectionResult | None:
        if not ctx.execution_layer:
            return None
        try:
            snap = ctx.execution_layer.get_portfolio_snapshot()
            # PortfolioSnapshot 是 dataclass (见 execution_layer/stats/display.py),
            # active_positions 为 int 类型, positions 为 list
            positions = getattr(snap, "positions", []) if snap else []
            if not positions:
                return SectionResult(name=self.name, title="价格预警", body="无活跃持仓")
            alerts = []
            for pos in positions:
                # positions 元素可能是 dataclass 或 dict, 同时兼容
                def _g(obj, key, default=None):
                    if isinstance(obj, dict):
                        return obj.get(key, default)
                    return getattr(obj, key, default)
                pnl = _g(pos, "pnl_pct", 0) or 0
                code = _g(pos, "code", "")
                name = _g(pos, "name", "")
                if pnl < -5:
                    alerts.append(f"  ⚠️ {code} {name}: 亏损 {pnl:.1f}% (止损线)")
                elif pnl > 15:
                    alerts.append(f"  ✅ {code} {name}: 盈利 {pnl:.1f}% (止盈线)")
            if not alerts:
                return SectionResult(name=self.name, title="价格预警", body="所有持仓在正常范围内")
            return SectionResult(name=self.name, title="价格预警", body="\n".join(alerts))
        except Exception as e:
            return SectionResult(name=self.name, title="价格预警", body=f"查询失败: {e}")


class VolumeAlertSection:
    name = "volume_alert"

    def build(self, ctx: ReportContext) -> SectionResult | None:
        if not ctx.data_layer:
            return None
        try:
            snap = ctx.data_layer.snapshot(ctx.date)
            if not snap or not snap.klines:
                return SectionResult(name=self.name, title="量能预警", body="无数据")
            alerts = []
            for k in snap.klines[:50]:
                vol_ratio = getattr(k, "volume_ratio", 1.0)
                if vol_ratio and vol_ratio > 3:
                    alerts.append(f"  📊 {k.code}: 放量 {vol_ratio:.1f}x")
            if not alerts:
                return SectionResult(name=self.name, title="量能预警", body="无量能异动")
            return SectionResult(name=self.name, title="量能预警", body=f"量能异动 ({len(alerts)} 只):\n" + "\n".join(alerts[:10]))
        except Exception as e:
            return SectionResult(name=self.name, title="量能预警", body=f"查询失败: {e}")


class IntradayAlertBuilder(ReportBuilder):
    @property
    def content_type(self) -> str:
        return "intraday_alert"

    @property
    def title(self) -> str:
        return "⚡ QuantStar 盘中异动"

    @property
    def severity(self) -> str:
        return "warning"

    def sections(self) -> list:
        return [IntradayAlertSection(), PriceAlertSection(), VolumeAlertSection()]
