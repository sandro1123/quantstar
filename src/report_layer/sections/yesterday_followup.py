"""R1: 🧵 昨日候选, 现在如何 Section — 连续性段 (设计稿-C §2.2 第 3 段)。

昨日每只候选一句话结局:
    已触价买入 — 昨日 entry_date 落库 (active/exit_pending/…)
    计划继续   — 未触价且 valid_until 未过期 (余 N 天)
    过期作废   — valid_until 已过 (诚实水位, 绝不静默消失)

Top N 收敛 (morning-topn-converge, 裁定#5): 逐只行限 Top 3 (触价者优先),
超出降一行计数「另有 N 只（触价 X / 继续 Y / 作废 Z），完整见深钻页 P0」;
头行总数不缩水 (诚实水位不丢票)。

数据 = 生命线 (stock_pool 状态 + 事件流), 昨日 = context.date 前一自然日
(交易日口径由 valid_until/entry_date 落库语义保证, 无需交易日历)。
无昨日候选时返回 None, 该段自动隐身。只读, 零写入 (铁律#4)。
"""
from __future__ import annotations

from datetime import date as _date, timedelta

from common.logging import get_logger
from report_layer.lifeline import build_lifelines
from report_layer.sections.base import Section, SectionResult
from report_layer.sections.base import note_degraded

logger = get_logger("report.section.yesterday_followup")

# 裁定#5 (morning-topn-converge): 逐只结局限 Top 3, 超出降一行计数
_TOP_N = 3


def _prev_day(as_of: str) -> str:
    try:
        return (_date.fromisoformat(as_of) - timedelta(days=1)).isoformat()
    except ValueError as _deg_exc:
        note_degraded("yesterday_followup", _deg_exc)
        return ""


class YesterdayFollowupSection(Section):
    name = "yesterday_followup"

    def build(self, context) -> SectionResult | None:
        try:
            db = getattr(context.data_layer, "db", None) if context.data_layer else None
            if db is None:
                return None
            conn = db.get_connection()
            lifelines = build_lifelines(conn)
            yesterday = _prev_day(context.date)
            if not yesterday:
                return None

            # 结局三态归组 (优先级: 触价 > 继续 > 作废; 同类稳定保持 code 序)
            outcomes = []  # (优先级, 文本, 类别)
            for ll in lifelines:
                if ll.status == "exited":
                    continue
                # 昨日入场的 → 已触价买入
                if ll.entry_date and ll.entry_date <= yesterday:
                    outcomes.append((0, f"✅ {ll.name}({ll.code})：昨日已触价买入 "
                                        f"{ll.entry_price:.2f}，跟踪中", "触价"))
                    continue
                # 昨日已在池 (plan_date/added_date <= 昨日) 的候选才有「结局」
                anchor = ll.plan_date or ll.added_date or ""
                if not anchor or anchor > yesterday:
                    continue
                if ll.valid_until and ll.valid_until < context.date:
                    outcomes.append((2, f"⚪ {ll.name}({ll.code})：昨日到期未触发，"
                                        f"计划作废", "作废"))
                else:
                    left = ""
                    if ll.valid_until:
                        try:
                            left = f"，余 {(_date.fromisoformat(ll.valid_until) - _date.fromisoformat(context.date)).days} 天"
                        except ValueError:
                            left = ""
                    outcomes.append((1, f"⏳ {ll.name}({ll.code})：昨日未触价，"
                                        f"计划继续{left}", "继续"))
            if not outcomes:
                return None
            outcomes.sort(key=lambda o: o[0])
            # 裁定#5: 逐只限 Top 3, 超出降一行计数 (头行总数不缩水)
            listed, folded = outcomes[:_TOP_N], outcomes[_TOP_N:]
            lines = [text for _p, text, _k in listed]
            if folded:
                buckets = {}
                for _p, _text, kind in folded:
                    buckets[kind] = buckets.get(kind, 0) + 1
                parts = " / ".join(
                    f"{k} {buckets[k]}" for k in ("触价", "继续", "作废")
                    if buckets.get(k))
                lines.append(f"另有 {len(folded)} 只（{parts}），"
                             f"完整见深钻页 P0")
            body = "\n".join([f"🧵 昨日候选, 现在如何 ({len(outcomes)} 只)",
                              "=" * 40, *lines])
            return SectionResult(title="🧵 昨日候选, 现在如何", body=body,
                                 name="yesterday_followup",
                                 sources=["lifeline"])
        except Exception as e:
            logger.warning(f"YesterdayFollowupSection build failed: {e}")
            return None
