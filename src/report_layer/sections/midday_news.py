"""午间要闻 Section — 东方财富等新闻源 (对齐旧脚本, 统一管线此前缺失)"""
from common.logging import get_logger
from report_layer.sections.base import Section, SectionResult

logger = get_logger("report.section.midday_news")

class MiddayNewsSection(Section):
    name = "midday_news"

    def build(self, context) -> SectionResult | None:
        # DL-40: 经 DataLayer 读 API 取新闻 (收编 NewsCollector 直连)。
        try:
            dl = context.data_layer
            if dl is None:
                return SectionResult(title="📰 午间要闻", body="暂无重要新闻",
                                     sources=["news"])
            items = (dl.get_intraday_news(context.date, limit=5,
                                          llm_service=getattr(context, "llm_service", None)) or [])[:3]
            if not items:
                return SectionResult(title="📰 午间要闻", body="暂无重要新闻",
                                     sources=["news"])
            lines = []
            for i, item in enumerate(items, 1):
                title = item.get("title", "")
                summary = (item.get("summary", "") or "").strip()[:60]
                lines.append(f"{i}. {title}")
                if summary:
                    lines.append(f"   {summary}")
            return SectionResult(title="📰 午间要闻", body="\n".join(lines),
                                 sources=["news"])
        except Exception as e:
            logger.warning(f"Midday news failed: {e}")
            return SectionResult(title="📰 午间要闻", body="暂无重要新闻")
