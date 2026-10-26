"""重点观察 Section — 含 WatchPool 数据"""
from report_layer.sections.base import Section, SectionResult
from report_layer.sections.base import note_degraded


class WatchFocusSection(Section):
    name = "watch_focus"

    def _get_watch_pool(self, context) -> list | None:
        if not context.execution_layer:
            return None
        try:
            stock_pool = context.execution_layer.stock_pool
            if not stock_pool:
                return None
            watch = stock_pool.get_by_status("watch")
            return watch or []
        except Exception as _deg_exc:
            note_degraded("watch_focus", _deg_exc)
            return None

    def build(self, context) -> SectionResult | None:
        try:
            watch = self._get_watch_pool(context)
            if watch is None:
                return SectionResult(title="重点观察", body="执行层不可用")
            if not watch:
                return SectionResult(title="重点观察", body="✅ 无观察中股票")
            sorted_watch = sorted(watch, key=lambda x: abs(x.get("initial_score", 0) or 0), reverse=True)
            lines = [f"🔭 重点观察 ({len(sorted_watch)} 只)"]
            lines.append("=" * 40)
            for item in sorted_watch[:10]:
                code = item.get("code", "")
                name = item.get("name", "") or ""
                score = item.get("initial_score", 0) or 0
                reason = item.get("watch_reason", "") or ""
                days = item.get("days_in_watch", 0) or 0
                entry_ready = "✅ 可入场" if score > 0.6 else "⏳ 待观察"
                lines.append(f"  {code} {name}")
                lines.append(f"    评分: {score:.2f} | 观察: {days}天 | {entry_ready}")
                if reason:
                    lines.append(f"    原因: {reason}")
            return SectionResult(title="重点观察", body="\n".join(lines))
        except Exception:
            return SectionResult(title="重点观察", body="数据不可用")
