# -*- coding: utf-8 -*-
"""板块分析+热点新闻 (宪法 #4): sector 沿用 sector_analysis 节, 新闻加 code 关联筛选。"""
from __future__ import annotations

from common.logging import get_logger
from report_layer.sections.base import Section, SectionResult
from report_layer.sections.base import note_degraded

logger = get_logger("report.section.sector_news")


class SectorNewsSection(Section):
    name = "sector_news"

    def build(self, context) -> SectionResult | None:
        try:
            db = getattr(context.data_layer, "db", None) if context.data_layer else None
            if db is None:
                return None
            conn = db.get_connection()
            lines = ["📡 板块与新闻", "=" * 40]
            have_any = False
            # 板块沿用 sector_analysis 数据面 (industry 分布)
            try:
                sect = conn.execute(
                    "SELECT s.industry, COUNT(*) n FROM daily_klines k "
                    "JOIN stock_info s ON s.code = k.code "
                    "WHERE k.date = ? AND k.change_pct > 5 GROUP BY s.industry "
                    "ORDER BY n DESC LIMIT 3", (context.date,)).fetchall()
                for ind, n in sect:
                    lines.append(f"🔹 {ind}: 今日 {n} 只涨超 5%")
                    have_any = True
            except Exception as _deg_exc:
                note_degraded("sector_news", _deg_exc)
                pass
            # 新闻: 只取有 code 关联的 (一条一动作格式)
            # F-241 判例: 原写 `publish_time`，但 stock_news 表真实列是 `date`
            # (news_collector.py:97 建表口径)，列名错配已致该段连续 10 个
            # 交易日静默降级 (2026-11-02~11-13 实测，OperationalError 被
            # except 吞掉后 note_degraded)。修法=对齐真实列名。
            try:
                news = conn.execute(
                    "SELECT code, title FROM stock_news "
                    "WHERE date >= ? AND code IS NOT NULL AND code != '' "
                    "ORDER BY date DESC LIMIT 3",
                    (context.date,)).fetchall()
                for code, title in news:
                    lines.append(f"📰 {code}: {title} — 关注对明日开盘的传导")
                    have_any = True
            except Exception as _deg_exc:
                note_degraded("sector_news", _deg_exc)
                pass
            if not have_any:
                lines.append("板块与新闻数据积累中")
            return SectionResult(title="📡 板块与新闻",
                                 body="\n".join(lines), name=self.name,
                                 sources=["daily_klines", "stock_news"])
        except Exception as e:
            logger.warning(f"SectorNewsSection build failed: {e}")
            return SectionResult(title="📡 板块与新闻", body="数据积累中",
                                  name=self.name)
