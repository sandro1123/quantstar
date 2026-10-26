"""R1: ⚠️ 风险提示 + 合规固定段 (设计稿-C §2.2 第 5 段, 范式 8)。

复用 RiskTipsSection 的风险条目, 尾部追加合规固定段:
「模拟盘研究性质, 非投资建议」每报必带。
"""
from __future__ import annotations

from common.logging import get_logger
from report_layer.sections.base import Section, SectionResult
from report_layer.sections.risk_tips import RiskTipsSection

logger = get_logger("report.section.risk_compliance")

COMPLIANCE_LINE = "📌 本系统为模拟盘研究性质，非投资建议。"


class RiskComplianceSection(Section):
    name = "risk_compliance"

    def build(self, context) -> SectionResult | None:
        try:
            tips = RiskTipsSection().build(context)
            body = (tips.body.strip() if tips and tips.body else "✅ 暂无显著风险")
            sources = list(tips.sources) if tips and tips.sources else ["rule-based"]
            return SectionResult(
                title="⚠️ 风险提示",
                body=f"{body}\n\n{COMPLIANCE_LINE}",
                name="risk_compliance",
                sources=sources)
        except Exception as e:
            logger.warning(f"RiskComplianceSection build failed: {e}")
            return SectionResult(title="⚠️ 风险提示", body=COMPLIANCE_LINE,
                                 name="risk_compliance")
