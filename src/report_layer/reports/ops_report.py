"""运维日报 — 17:30 推送"""
from report_layer.reports.base import ReportBuilder, ReportContext
from report_layer.sections.base import SectionResult
from report_layer.sections.risk_tips import RiskTipsSection


class OpsSummarySection:
    name = "ops_summary"

    def build(self, ctx: ReportContext) -> SectionResult | None:
        if not ctx.ops_layer:
            return None
        try:
            report = ctx.ops_layer.check_health()
            pipeline_logger = getattr(ctx.ops_layer, "pipeline_logger", None)
            stats = {"total": 0, "success": 0, "failed": 0, "skipped": 0}
            if pipeline_logger:
                try:
                    runs = pipeline_logger.get_recent_runs(days=1)
                    stats["total"] = len(runs)
                    for r in runs:
                        if r.status == "success":
                            stats["success"] += 1
                        elif r.status == "failed":
                            stats["failed"] += 1
                        elif r.status == "skipped":
                            stats["skipped"] += 1
                except Exception:
                    pass
            return SectionResult(
                title="运维概览",
                body=(
                    f"整体状态: {report.overall_status}\n"
                    f"关注项: {len(report.needs_attention)} 项\n"
                    f"管道运行: {stats['total']} 次, "
                    f"成功: {stats['success']}, "
                    f"失败: {stats['failed']}, "
                    f"跳过: {stats['skipped']}\n"
                ),
            )
        except Exception as e:
            return SectionResult(name=self.name, title="运维概览", body=f"查询失败: {e}")


class PipelineHealthSection:
    name = "pipeline_health"

    def build(self, ctx: ReportContext) -> SectionResult | None:
        if not ctx.ops_layer:
            return None
        try:
            pipeline_logger = getattr(ctx.ops_layer, "pipeline_logger", None)
            if not pipeline_logger:
                return SectionResult(name=self.name, title="管道健康", body="pipeline_logger 不可用")
            recent = pipeline_logger.get_recent_runs(days=1) or []
            if not recent:
                return SectionResult(name=self.name, title="管道健康", body="无运行记录")
            lines = ["最近管道运行:"]
            for r in recent[:10]:
                lines.append(f"  {r.pipeline_name}: {r.status} ({r.duration_s or 0:.1f}s)")
            return SectionResult(name=self.name, title="管道健康", body="\n".join(lines))
        except Exception as e:
            return SectionResult(name=self.name, title="管道健康", body=f"查询失败: {e}")


class DataLagSection:
    name = "data_lag"

    def build(self, ctx: ReportContext) -> SectionResult | None:
        if not ctx.data_layer:
            return None
        try:
            lag = getattr(ctx.data_layer, "get_lag_minutes", lambda: 0)()
            return SectionResult(
                title="数据延迟",
                body=f"当前数据延迟: {lag} 分钟\n" + ("✅ 正常" if lag < 15 else "⚠️ 需要关注" if lag < 60 else "❌ 严重延迟"),
            )
        except Exception:
            return SectionResult(name=self.name, title="数据延迟", body="查询失败")


class OpsReportBuilder(ReportBuilder):
    @property
    def content_type(self) -> str:
        return "ops_report"

    @property
    def title(self) -> str:
        return "🔧 QuantStar 运维日报"

    def sections(self) -> list:
        return [OpsSummarySection(), PipelineHealthSection(), DataLagSection(), RiskTipsSection()]
