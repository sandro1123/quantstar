"""R3: 📊 市场一句话 Section — 替换 MarketSummary 的数据量统计 (设计稿-C §2.5 第 4 段)。

旧 MarketSummary 是纯日志段 (K线/估值/财务数据量统计), 对用户无行动价值, 删除。
改为指数+涨跌基调压缩一句话。
"""
from __future__ import annotations

from common.logging import get_logger
from report_layer.sections.base import Section, SectionResult

logger = get_logger("report.section.market_one_liner")


class MarketOneLinerSection(Section):
    name = "market_one_liner"

    def build(self, context) -> SectionResult | None:
        try:
            try:
                from report_layer.sections.indices import fetch_realtime_indices
                idx = fetch_realtime_indices(context.data_layer)[:3]
            except Exception:
                idx = []
            if not idx:
                return SectionResult(title="📊 市场一句话",
                                     body="今日市场数据暂不可用",
                                     name=self.name)
            parts = [f"{i.get('name', '')} {i.get('price', 0)}"
                     f"（{(i.get('change_pct', 0) or 0):+.2f}%）" for i in idx]
            up = sum(1 for i in idx if (i.get("change_pct", 0) or 0) > 0)
            tone = "偏多" if up > len(idx) / 2 else "偏空" if up < len(idx) / 2 else "震荡"
            return SectionResult(title="📊 市场一句话",
                                 body=f"{'；'.join(parts)}，整体{tone}。",
                                 name=self.name,
                                 sources=["realtime"])
        except Exception as e:  # noqa: BLE001
            logger.warning(f"MarketOneLiner build failed: {e}")
            return SectionResult(title="📊 市场一句话",
                                 body="今日市场数据暂不可用",
                                 name=self.name)
