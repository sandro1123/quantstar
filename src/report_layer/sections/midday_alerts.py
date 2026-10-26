"""上午异动 Section — 涨跌停 (Fuyao 涨跌停池实时)

S5回归修复: 原实现读 snapshot.klines 筛 ±9.8%, 但盘中当日 kline 未落库恒为空。
改为走 Fuyao 涨跌停池。注意: 涨停/跌停是两个独立端点 (limit-up-pool /
limit-down-pool), item 内无 "type" 字段, 不能按 type 过滤 (旧实现恒为空)。
"""
from common.logging import get_logger
from report_layer.sections.base import Section, SectionResult

logger = get_logger("report.section.midday_alerts")

class MiddayAlertsSection(Section):
    name = "midday_alerts"

    def _get_limit_up_down(self, context):
        # DL-40: 经 DataLayer 读 API 取涨跌停池 (收编 FuyaoClient 直连)。
        # 诚实降级: 返回空列表, 不 fallback 直连, 不造假值。
        try:
            dl = context.data_layer
            if dl is None:
                logger.warning("MiddayAlerts: data_layer 不可用")
                return [], []
            pool = dl.get_limit_pool(context.date)
            return pool.get("up", []), pool.get("down", [])
        except Exception as e:
            logger.warning(f"Limit pool failed: {e}")
            return [], []

    def build(self, context) -> SectionResult | None:
        up, down = self._get_limit_up_down(context)
        lines = ["🚀 上午异动"]
        if up:
            lines.append("")
            lines.append(f"🔴 涨停 ({len(up)} 只)")
            # 连板数高的排前, 更能反映市场情绪
            up_sorted = sorted(up, key=lambda d: d.get("count", 0) or 0,
                               reverse=True)
            for item in up_sorted[:5]:
                name = item.get("name", item.get("code", ""))
                count = item.get("count", 0) or 0
                suffix = f" [{count}连板]" if count > 1 else ""
                lines.append(f"  {name}{suffix}")
        if down:
            lines.append("")
            lines.append(f"🟢 跌停 ({len(down)} 只)")
            for item in down[:5]:
                name = item.get("name", item.get("code", ""))
                pct = item.get("change_pct", 0) or 0
                lines.append(f"  {name} {pct:.2f}%")
        if not up and not down:
            lines.append("")
            lines.append("暂无涨跌停数据")
        return SectionResult(title="🚀 上午异动", body="\n".join(lines),
                             sources=["fuyao"])
