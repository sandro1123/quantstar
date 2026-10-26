"""盘面总览 Section"""
from report_layer.sections.base import Section, SectionResult
from report_layer.sections.base import note_degraded


class MarketOverviewSection(Section):
    name = "market_overview"

    def build(self, context) -> SectionResult | None:
        try:
            snapshot = context.data_layer.snapshot(context.date) if context.data_layer else None
            klines = snapshot.klines if snapshot and snapshot.klines else []
            if not klines:
                return None
            total = len(klines)
            up = sum(1 for k in klines if getattr(k, "change_pct", 0) > 0)
            down = sum(1 for k in klines if getattr(k, "change_pct", 0) < 0)
            flat = total - up - down
            body = f"📊 市场概览 ({context.date})\n上涨 {up} | 下跌 {down} | 平盘 {flat} | 共 {total} 只"
            return SectionResult(title="盘面总览", body=body)
        except Exception as _deg_exc:
            note_degraded("market_overview", _deg_exc)
            return None
