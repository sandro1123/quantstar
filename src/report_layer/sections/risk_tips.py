"""风险提示 Section (F5.3: LLM 风险提示 + 规则降级)"""
from report_layer.sections.base import Section, SectionResult
from report_layer.sections._llm_helper import llm_call
from report_layer.sections.base import note_degraded


class RiskTipsSection(Section):
    name = "risk_tips"

    def build(self, context) -> SectionResult | None:
        try:
            snapshot = context.data_layer.snapshot(context.date) if context.data_layer else None
            klines = snapshot.klines if snapshot and snapshot.klines else []
            risks = []
            down_count = 0
            if klines:
                down_count = sum(1 for k in klines if getattr(k, "change_pct", 0) < -3)
                if down_count > len(klines) * 0.3:
                    risks.append(f"⚠️ {down_count} 只股票跌幅超 3%")
            if not risks:
                risks.append("✅ 暂无显著风险")

            # F5.3: LLM 风险提示 (失败 → 仅规则结果, 不阻塞)
            prompt = (
                f"今日A股: 共 {len(klines)} 只, 跌幅超3%的有 {down_count} 只。\n"
                f"请给出1-2条今日最需要注意的风险提示，不要markdown，每条一行。"
            )
            llm_tips = llm_call(context, prompt, call_point="risk_tips",
                                temperature=0.4, max_tokens=2000)
            if llm_tips:
                risks.append("")
                risks.append(f"🧠 {llm_tips}")
                return SectionResult(title="风险提示", body="\n".join(risks),
                                     sources=["rule-based", "LLM"])
            return SectionResult(title="风险提示", body="\n".join(risks),
                                 sources=["rule-based"])
        except Exception as _deg_exc:
            note_degraded("risk_tips", _deg_exc)
            return None
