"""告警报告"""
from report_layer.reports.base import ReportBuilder, ReportContext
from report_layer.sections.base import SectionResult


class AlertSection:
    name = "alerts"

    def build(self, ctx: ReportContext) -> SectionResult | None:
        if not ctx.ops_layer:
            return None
        try:
            alert_manager = ctx.ops_layer.alert_manager
            alerts = alert_manager.get_open_alerts()
            if not alerts:
                return SectionResult(name=self.name, title="当前告警", body="✅ 无未处理告警")
            critical = [a for a in alerts if a.severity == "critical"]
            warning = [a for a in alerts if a.severity == "warning"]
            info = [a for a in alerts if a.severity == "info"]
            lines = [f"告警汇总: {len(critical)} 严重, {len(warning)} 警告, {len(info)} 信息\n"]
            if critical:
                lines.append("【严重】---")
                for a in critical[:5]:
                    lines.append(f"  🔴 {a.layer}: {a.title} — {a.detail}")
            if warning:
                lines.append("【警告】---")
                for a in warning[:5]:
                    lines.append(f"  🟡 {a.layer}: {a.title} — {a.detail}")
            if info:
                lines.append("【信息】---")
                for a in info[:5]:
                    lines.append(f"  🔵 {a.layer}: {a.title} — {a.detail}")
            return SectionResult(name=self.name, title="当前告警", body="\n".join(lines))
        except Exception as e:
            return SectionResult(name=self.name, title="当前告警", body=f"告警查询失败: {e}")


class AlertHistorySection:
    name = "alert_history"

    def build(self, ctx: ReportContext) -> SectionResult | None:
        if not ctx.ops_layer:
            return None
        try:
            alert_manager = ctx.ops_layer.alert_manager
            history = alert_manager.get_recent_history(limit=10)
            if not history:
                return SectionResult(name=self.name, title="告警历史", body="无历史告警记录")
            lines = [f"最近 {len(history)} 条告警:"]
            for h in history:
                sev = {"critical": "🔴", "warning": "🟡", "info": "🔵"}.get(h.severity, "⚪")
                lines.append(f"  {sev} [{h.created_at}] {h.layer}: {h.title}")
            return SectionResult(name=self.name, title="告警历史", body="\n".join(lines))
        except Exception as e:
            return SectionResult(name=self.name, title="告警历史", body=f"查询失败: {e}")


class AlertRuleSection:
    name = "alert_rules"

    def build(self, ctx: ReportContext) -> SectionResult | None:
        if not ctx.ops_layer:
            return None
        try:
            rules = getattr(ctx.ops_layer, "alert_manager", None)
            if not rules or not hasattr(rules, "get_rules"):
                return SectionResult(name=self.name, title="告警规则", body="规则管理不可用")
            rule_list = rules.get_rules()
            if not rule_list:
                return SectionResult(name=self.name, title="告警规则", body="无配置规则")
            lines = [f"告警规则 ({len(rule_list)} 条):"]
            for r in rule_list:
                lines.append(f"  {r.name}: {r.severity} — {r.description}")
            return SectionResult(name=self.name, title="告警规则", body="\n".join(lines))
        except Exception:
            return SectionResult(name=self.name, title="告警规则", body="查询失败")


class AlertBuilder(ReportBuilder):
    @property
    def content_type(self) -> str:
        return "alerts"

    @property
    def title(self) -> str:
        return "🔔 QuantStar 告警"

    @property
    def severity(self) -> str:
        return "warning"

    def sections(self) -> list:
        return [AlertSection(), AlertHistorySection(), AlertRuleSection()]
