"""上午盘面 Section — 指数表现 + 涨跌统计 (走实时源)

S5回归修复: 原实现读 data_layer.snapshot(date) 的 market 字段,
但午报在盘中 (12:00) 运行时当日 kline 尚未落库 (15:30 收盘后才写),
导致涨跌家数恒为 0。改为: 指数走 gtimg 实时; 涨跌家数优先用 snapshot
(收盘后/回填时有效), 盘中为空时降级为基于指数的涨跌基调, 不再显示误导的 0。
"""
from common.logging import get_logger
from report_layer.sections.base import Section, SectionResult
from report_layer.sections.base import note_degraded

logger = get_logger("report.section.midday_market")

class MiddayMarketSection(Section):
    name = "midday_market"

    def _get_indices(self, context):
        try:
            from report_layer.sections.indices import fetch_realtime_indices
            return [
                {"name": idx.get("name", ""), "price": idx.get("price", 0) or 0,
                 "pct": idx.get("change_pct", 0) or 0}
                for idx in fetch_realtime_indices(context.data_layer)[:5]
            ]
        except Exception as e:
            logger.warning(f"realtime indices failed: {e}")
        return []

    def _get_market_stats(self, context):
        """收盘后/回填时 snapshot.market 有效; 盘中为空返回 None。"""
        try:
            snap = context.data_layer.snapshot(context.date) if context.data_layer else None
            if snap and getattr(snap, "market", None):
                m = snap.market
                up = getattr(m, "up_count", 0) or 0
                down = getattr(m, "down_count", 0) or 0
                flat = getattr(m, "flat_count", 0) or 0
                if up + down + flat > 0:
                    return up, down, flat
        except Exception as _deg_exc:
            note_degraded("midday_market", _deg_exc)
            pass
        return None

    def build(self, context) -> SectionResult | None:
        indices = self._get_indices(context)
        stats = self._get_market_stats(context)
        if not indices and stats is None:
            return SectionResult(title="📊 上午盘面", body="数据暂不可用",
                                 sources=["gtimg"])
        lines = []
        for idx in indices:
            pct = idx["pct"]
            color = "🔴" if pct > 0 else "🟢"
            arrow = "↑" if pct > 0 else "↓"
            lines.append(f"{idx['name']}: {idx['price']:,.2f} {color} {arrow}{abs(pct):.2f}%")
        if stats is not None:
            up, down, flat = stats
            total = up + down + flat
            lines.append("")
            lines.append(f"涨: {up} 跌: {down} 平: {flat} 共 {total} 只")
            if down > 0:
                lines.append(f"涨跌比: {up / down:.2f}")
        elif indices:
            # 盘中无全市场涨跌家数 → 用指数涨跌给基调 (对齐旧脚本)
            up_n = sum(1 for i in indices if i["pct"] > 0)
            down_n = sum(1 for i in indices if i["pct"] < 0)
            tone = "上涨为主" if up_n > down_n else ("下跌为主" if down_n > up_n else "涨跌互现")
            lines.append("")
            lines.append(f"上午{tone}，主要指数 {up_n} 涨 {down_n} 跌")
        return SectionResult(title="📊 上午盘面", body="\n".join(lines) if lines else "数据暂不可用",
                             sources=["gtimg"])
