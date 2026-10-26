"""Agent 综合判断 Section — 大盘方向/仓位建议"""
from common.logging import get_logger
from report_layer.sections.base import Section, SectionResult
from agent_layer import build_request

logger = get_logger("report.section.agent_plan")


class AgentTradePlanSection(Section):
    name = "agent_trade_plan"

    def build(self, context) -> SectionResult | None:
        try:
            if not context.agent_layer:
                return SectionResult(title="🤖 Agent 综合判断", body="Agent层未接入")
            market_code = "000001.SH"
            # AG-WIRE-1: quick_scan 是管线名非档位值 (合法档 quick/normal/deep);
            # depth=quick_scan 恰命中 AgentLayer.analyze 的 memory 早退分支
            # (write_prediction 之前 return) — 改 depth=quick + 删 input 冗余键。
            request = build_request(
                task="market_analysis",
                input={"code": market_code, "date": context.date},
                depth="quick",
            )
            response = context.agent_layer.analyze(request)
            lines = [f"🤖 Agent 综合判断 ({context.date})"]
            lines.append("=" * 40)
            lines.append(f"📊 判断: {response.verdict}")
            lines.append(f"🎯 置信度: {response.confidence:.1%}")
            if response.reasoning:
                lines.append(f"💡 理由: {response.reasoning}")
            if response.risk_flags:
                lines.append("⚠️ 风险提示:")
                for rf in response.risk_flags:
                    flag = rf.flag if hasattr(rf, "flag") else str(rf)
                    sev = rf.severity if hasattr(rf, "severity") else "info"
                    icon = "🔴" if sev == "high" else ("🟡" if sev == "medium" else "⚪")
                    lines.append(f"  {icon} {flag}")
            position_pct = 0.5
            if hasattr(response, "output") and isinstance(response.output, dict):
                position_pct = response.output.get("position_pct", position_pct)
            lines.append(f"\n💼 建议仓位: {position_pct:.0%}")
            return SectionResult(title="🤖 Agent 综合判断", body="\n".join(lines))
        except Exception as e:
            logger.warning(f"AgentTradePlanSection build failed: {e}")
            return SectionResult(title="🤖 Agent 综合判断", body="Agent暂不可用")
