# -*- coding: utf-8 -*-
"""今日大盘总结 (宪法 #4): 涨跌家数/量能/情绪位置 3~4 行, LLM 润色 researcher 风格。"""
from __future__ import annotations

from common.logging import get_logger
from report_layer.sections._llm_helper import llm_call
from report_layer.sections.base import Section, SectionResult

logger = get_logger("report.section.market_wrap")


class MarketWrapSection(Section):
    name = "market_wrap"

    def build(self, context) -> SectionResult | None:
        try:
            db = getattr(context.data_layer, "db", None) if context.data_layer else None
            if db is None:
                return None
            conn = db.get_connection()
            row = conn.execute(
                "SELECT COUNT(*) n, "
                "SUM(CASE WHEN change_pct > 0 THEN 1 ELSE 0 END) up, "
                "SUM(CASE WHEN change_pct < 0 THEN 1 ELSE 0 END) down, "
                "AVG(volume) avg_vol FROM daily_klines WHERE date = ?",
                (context.date,),
            ).fetchone()
            n, up, down, avg_vol = (row[0], row[1] or 0, row[2] or 0, row[3] or 0)
            if not n:
                return SectionResult(
                    title="📊 今日大盘", body="今日行情数据积累中",
                    name=self.name)
            breadth = up / n if n else 0
            emotion = ("偏强" if breadth > 0.55 else
                       "偏弱" if breadth < 0.45 else "均衡")
            base = (
                f"📊 今日大盘总结\n"
                f"{'=' * 40}\n"
                f"涨跌家数：上涨 {up} 家 / 下跌 {down} 家（涨占比 {breadth:.0%}）\n"
                f"量能：市场日均量 {avg_vol / 1e6:.1f} 百万股量级\n"
                f"情绪位置：{emotion}"
            )
            polished = llm_call(
                context, f"用 researcher 风格 1-2 句润色今日 A 股总结，"
                         f"上涨{up}家/下跌{down}家，情绪{emotion}，不要编数据",
                call_point="market_wrap", style="researcher") if hasattr(
                llm_call, "__call__") else None
            body = base + (f"\n{polished}" if polished else "")
            return SectionResult(title="📊 今日大盘", body=body, name=self.name,
                                 sources=["daily_klines"])
        except Exception as e:
            logger.warning(f"MarketWrapSection build failed: {e}")
            return SectionResult(title="📊 今日大盘", body="大盘数据积累中",
                                  name=self.name)
