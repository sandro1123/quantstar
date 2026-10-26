"""持仓动态 Section"""
from report_layer.sections.base import Section, SectionResult
from report_layer.sections.base import note_degraded


class PositionDynamicSection(Section):
    name = "position_dynamic"

    def build(self, context) -> SectionResult | None:
        try:
            if not context.execution_layer:
                return None
            snapshot = context.execution_layer.get_portfolio_snapshot()
            active = snapshot.active
            pending = snapshot.pending_entry
            body = f"📈 持仓 {active} 只 | 待入场 {pending} 只"
            return SectionResult(title="持仓动态", body=body)
        except Exception as _deg_exc:
            note_degraded("position_dynamic", _deg_exc)
            return None
