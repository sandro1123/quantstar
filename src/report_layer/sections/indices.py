"""大盘指数 Section — gtimg 实时指数 (复用 data_layer GTimgSourceAdapter)

F5.1: 从旧脚本 build_morning_brief.build_indices 提取, 统一走 data_layer 数据源。
降级: gtimg 失败 → "指数数据暂不可用" 而非报错。
"""
from common.logging import get_logger
from report_layer.sections.base import Section, SectionResult

logger = get_logger("report.section.indices")


def fetch_realtime_indices(data_layer=None) -> list:
    """实时大盘指数 (共享函数, 供 IndicesSection / MiddayMarketSection 复用)

    DL-40: 经 DataLayer 读 API get_realtime_indices (走 SourceRegistry, gtimg 主源)。
    返回 [{"name", "price", "change_pct"}]; 失败/data_layer 不可用返回 []。
    """
    if data_layer is None:
        logger.warning("fetch_realtime_indices: data_layer 不可用")
        return []
    try:
        return data_layer.get_realtime_indices() or []
    except Exception as e:
        logger.warning(f"Index fetch failed: {e}")
        return []


class IndicesSection(Section):
    name = "indices"

    def build(self, context) -> SectionResult | None:
        try:
            indices = fetch_realtime_indices(context.data_layer)
            if not indices:
                return SectionResult(title="📊 大盘指数", body="指数数据暂不可用",
                                     sources=["gtimg"])
            lines = []
            for idx in indices[:5]:
                pct = idx.get("change_pct", 0) or 0
                # A股惯例: 涨红跌绿
                color = "🔴" if pct > 0 else "🟢"
                arrow = "↑" if pct > 0 else "↓"
                lines.append(f"{idx.get('name', '')}: {idx.get('price', 0):,.2f} "
                             f"{color} {arrow}{abs(pct):.2f}%")
            return SectionResult(title="📊 大盘指数", body="\n".join(lines),
                                 sources=["gtimg"])
        except Exception as e:
            logger.warning(f"IndicesSection build failed: {e}")
            return SectionResult(title="📊 大盘指数", body="指数数据暂不可用")
