# -*- coding: utf-8 -*-
"""盘前速报竞价快评 (宪法 #7): 集合竞价扫描昨日推荐+观察池。

auction_snapshot 数据面; 缺竞价数据的票诚实「竞价数据积累中」。
"""
from __future__ import annotations

from datetime import date as _date, timedelta

from common.logging import get_logger
from report_layer.sections.base import Section, SectionResult
from report_layer.sections.base import note_degraded

logger = get_logger("report.section.premarket_review")


def _prev_day(as_of: str) -> str:
    try:
        return (_date.fromisoformat(as_of) - timedelta(days=1)).isoformat()
    except ValueError as _deg_exc:
        note_degraded("premarket_review", _deg_exc)
        return ""


class PremarketReviewSection(Section):
    name = "premarket_review"

    def build(self, context) -> SectionResult | None:
        try:
            db = getattr(context.data_layer, "db", None) if context.data_layer else None
            if db is None:
                return None
            conn = db.get_connection()
            yesterday = _prev_day(context.date)
            # 昨日推荐+观察池 = added_date=yesterday 或仍在有效期的 watch/pending
            pool = conn.execute(
                "SELECT code, name, entry_low, entry_high FROM stock_pool "
                "WHERE status IN ('watch', 'pending_entry', 'active') "
                "AND (added_date = ? OR valid_until IS NULL OR valid_until >= ?)",
                (yesterday, context.date)).fetchall()
            if not pool:
                return SectionResult(
                    title="⚡ 竞价快评",
                    body="观察池为空（推荐积累中）", name=self.name)
            lines = [f"⚡ 竞价快评（{len(pool)} 只）", "=" * 40]
            have_data = False
            for (code, name, low, high) in pool:
                label = f"{name}({code})" if name else code
                arow = conn.execute(
                    "SELECT auction_pct, auction_turnover_pct, auction_price "
                    "FROM auction_snapshot WHERE code = ? AND date = ?",
                    (code, context.date)).fetchone()
                if arow is None:
                    lines.append(f"⚪ {label}：竞价数据积累中")
                    continue
                apct, turnover, aprice = arow
                have_data = True
                guidance = self._guidance(apct, turnover, low, high, aprice)
                lines.append(
                    f"⚡ {label}：竞价 {'高开' if (apct or 0) >= 0 else '低开'} "
                    f"{apct:.1f}% 量比 {turnover:.1f} —— {guidance}")
            if not have_data:
                lines.append("（整体竞价数据积累中，9:30 后首拍复核）")
            return SectionResult(title="⚡ 竞价快评",
                                 body="\n".join(lines), name=self.name,
                                 sources=["auction_snapshot", "stock_pool"])
        except Exception as e:
            logger.warning(f"PremarketReviewSection build failed: {e}")
            return SectionResult(title="⚡ 竞价快评", body="竞价数据积累中",
                                  name=self.name)

    @staticmethod
    def _guidance(apct, turnover, low, high, aprice) -> str:
        try:
            if apct is not None and apct > 3 and turnover and turnover > 2:
                return "符合买入条件，9:30 后站稳可介入"
            if apct is not None and apct < -3:
                return "低开超 3%，观望为主，破位止损"
            if low and high and aprice and low <= aprice <= high:
                return "竞价落在建议价区，开盘确认量能后介入"
            return "竞价中性，按原计划价区观察"
        except (TypeError, ValueError):
            return "按原计划价区观察"
