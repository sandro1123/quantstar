"""R1: 🔴 回避卡 Section — ST/*ST 或极端风险候选不进买入位 (样张 3.3)。

一眼红=别碰。候选集合 = 生命线 ∈ {pending_entry, watch} 且 valid_until
未过期, 名称含 ST (含 *ST)。无回避对象时返回 None, 该段自动隐身。
只读, 零写入 (铁律#4)。
"""
from __future__ import annotations

from common.logging import get_logger
from report_layer.lifeline import build_lifelines, is_open
from report_layer.sections.base import Section, SectionResult

logger = get_logger("report.section.avoid_cards")

# 裁定#5 (morning-topn-converge): 回避卡 ≤3 只全列, 超出降一行计数
_TOP_N = 3


class AvoidCardsSection(Section):
    name = "avoid_cards"

    def build(self, context) -> SectionResult | None:
        try:
            db = getattr(context.data_layer, "db", None) if context.data_layer else None
            if db is None:
                return None
            conn = db.get_connection()
            lifelines = build_lifelines(conn)
            avoids = [
                ll for ll in lifelines
                if ll.status in ("pending_entry", "watch")
                and is_open(ll, context.date)
                and "ST" in (ll.name or "").upper()
            ]
            if not avoids:
                return None
            lines = [f"🔴 回避卡 ({len(avoids)} 只)"]
            lines.append("=" * 40)
            # 裁定#5 (morning-topn-converge): ≤3 只全列, 超出降一行计数指深钻页
            for ll in avoids[:_TOP_N]:
                lines.append(f"🔴 {ll.name}({ll.code})：不要买 — ST 风险股, "
                             f"退市/戴帽风险高, 系统不纳入推荐")
            rest = len(avoids) - _TOP_N
            if rest > 0:
                lines.append(f"另有 {rest} 只回避对象，深钻页 P0 全量")
            return SectionResult(title="🔴 回避卡", body="\n".join(lines),
                                 name="avoid_cards", sources=["lifeline"])
        except Exception as e:
            logger.warning(f"AvoidCardsSection build failed: {e}")
            return None
