"""今日要闻 Section — NewsCollector (复用 data_layer)

F5.1: 从旧脚本 build_morning_brief.build_news 提取, 统一走 data_layer NewsCollector。
先读当日已采集新闻, 为空则触发采集; 采集/读取失败 → "暂无重要新闻" 而非报错。
"""
from common.logging import get_logger
from report_layer.sections._llm_helper import get_llm
from report_layer.sections.base import Section, SectionResult

logger = get_logger("report.section.news")

_SENTIMENT_TAG = {"bullish": "[利好]", "bearish": "[利空]", "neutral": ""}


class NewsSection(Section):
    name = "news"

    def _get_items(self, context) -> list:
        # DL-40: 经 DataLayer 读 API 取新闻 (收编 NewsCollector 直连, DL-08 F-10⑦⑧)。
        # get_intraday_news 内部: 先读已采集 → 为空则触发一次采集 → 再读。
        # 诚实降级: 返回空列表, 不 fallback 直连, 不造假值。
        try:
            dl = context.data_layer
            if dl is None:
                logger.warning("NewsSection: data_layer 不可用")
                return []
            return dl.get_intraday_news(context.date, limit=8,
                                          llm_service=get_llm(context)) or []
        except Exception as e:
            logger.warning(f"News fetch failed: {e}")
            return []

    @staticmethod
    def _clean_summary(summary: str, title: str) -> str:
        s = (summary or "").strip()
        for prefix in (f"【{title[:20]}", f"「{title[:20]}", f"'{title[:20]}"):
            if s.startswith(prefix):
                end = s.find("】")
                if end > 0:
                    s = s[end + 1:].strip()
        return s

    def build(self, context) -> SectionResult | None:
        try:
            items = self._get_items(context)
            if not items:
                return SectionResult(title="📰 今日要闻", body="暂无重要新闻",
                                     sources=["news_collector"])
            lines = []
            for i, item in enumerate(items[:5], 1):
                title = item.get("title", "") or ""
                summary = self._clean_summary(item.get("summary", ""), title)
                tag = _SENTIMENT_TAG.get(item.get("sentiment", "neutral"), "")
                display = summary[:100] + "..." if len(summary) > 100 else summary
                lines.append(f"{i}. {title}  {tag}".rstrip())
                if display:
                    lines.append(f"   {display}")
            return SectionResult(title="📰 今日要闻", body="\n".join(lines),
                                 sources=["news_collector"])
        except Exception as e:
            logger.warning(f"NewsSection build failed: {e}")
            return SectionResult(title="📰 今日要闻", body="暂无重要新闻")
