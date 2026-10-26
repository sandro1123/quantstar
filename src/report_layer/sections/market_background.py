"""R1: 📊 市场背景 Section — 外盘+指数+要闻压缩合并一段 (设计稿-C §2.2 第 4 段)。

复用既有三段模块 (OvernightMarketSection / IndicesSection / NewsSection)
的 build 输出, 只改编排不改数据源; 全部不可用时诚实降级「暂不可用」。
"""
from __future__ import annotations

from common.logging import get_logger
from report_layer.sections.base import Section, SectionResult
from report_layer.sections.indices import IndicesSection
from report_layer.sections.news import NewsSection
from report_layer.sections.overnight_market import OvernightMarketSection

logger = get_logger("report.section.market_background")

_UNAVAILABLE_MARKS = ("暂不可用", "暂无重要新闻")


class MarketBackgroundSection(Section):
    name = "market_background"

    def build(self, context) -> SectionResult | None:
        try:
            parts: list[str] = []
            sources: list[str] = []
            for sub in (OvernightMarketSection(), IndicesSection(),
                        NewsSection()):
                try:
                    res = sub.build(context)
                except Exception as e:
                    logger.warning(f"market_background sub {sub.name} failed: {e}")
                    continue
                if res is None:
                    continue
                body = (res.body or "").strip()
                # 子段降级文案 (暂不可用/暂无) 不入压缩段, 避免噪声
                if not body or any(m in body for m in _UNAVAILABLE_MARKS):
                    continue
                parts.append(body)
                sources.extend(s for s in (res.sources or []) if s not in sources)
            if not parts:
                return SectionResult(title="📊 市场背景",
                                     body="市场背景数据暂不可用",
                                     name="market_background")
            return SectionResult(title="📊 市场背景",
                                 body="\n\n".join(parts),
                                 name="market_background",
                                 sources=sources)
        except Exception as e:
            logger.warning(f"MarketBackgroundSection build failed: {e}")
            return SectionResult(title="📊 市场背景",
                                 body="市场背景数据暂不可用",
                                 name="market_background")
