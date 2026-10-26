"""板块轮动 Section — 上午强势/弱势板块 (Fuyao 同花顺行业指数实时)

S5回归修复: 原实现读 snapshot.indices 筛 "板块", 但 DataSnapshot 无 indices 字段,
恒为空。改为直接走 Fuyao 行业指数目录 + 实时快照 (与旧脚本 build_sectors 同源)。
"""
from common.logging import get_logger
from report_layer.sections.base import Section, SectionResult

logger = get_logger("report.section.midday_sectors")

class MiddaySectorsSection(Section):
    name = "midday_sectors"

    def _fetch_sectors(self, context):
        # DL-40: 经 DataLayer 读 API 取板块轮动 (收编 FuyaoClient 直连)。
        # 诚实降级: 返回空列表, 不 fallback 直连, 不造假值。
        try:
            dl = context.data_layer
            if dl is None:
                logger.warning("MiddaySectors: data_layer 不可用")
                return []
            return dl.get_sector_rotation() or []
        except Exception as e:
            logger.warning(f"Sectors fetch failed: {e}")
            return []

    def build(self, context) -> SectionResult | None:
        sectors = self._fetch_sectors(context)
        if not sectors:
            return SectionResult(title="🎯 板块轮动", body="暂无板块数据",
                                 sources=["fuyao"])
        up = [s for s in sectors if s["pct"] > 0]
        down = [s for s in sectors if s["pct"] < 0]
        lines = ["🎯 板块轮动"]
        if up:
            lines.append("")
            lines.append("🔴 强势板块:")
            for s in up[:5]:
                lines.append(f"  {s['name']}: {s['pct']:+.2f}%")
        else:
            lines.append("")
            lines.append("🔴 强势板块: 无")
        if down:
            lines.append("")
            lines.append("🟢 弱势板块:")
            for s in sorted(down, key=lambda x: x["pct"])[:5]:
                lines.append(f"  {s['name']}: {s['pct']:+.2f}%")
        return SectionResult(title="🎯 板块轮动", body="\n".join(lines),
                             sources=["fuyao"])
