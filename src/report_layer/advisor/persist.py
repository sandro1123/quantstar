# -*- coding: utf-8 -*-
"""模块1b 产出落库接线 — advisor_text / advisor_guard 写回

病根（2026-10-09 取证）：`write_back_advisor` 生产零调用方，
`recommendation_rationale.advisor_text` 91/91 全空。渲染路径
`rewrite_to_advisor` 出了 result 从不落库 = 「建了没接线」又一变体
（读侧 consumers 全通、写侧断）。

铁律#12：写库失败绝不阻断渲染（落库是取证面，不是推荐主链）。
零新增 LLM 调用：只把已产出的 result 落库，不重算。
"""
from common.logging import get_logger

from .rationale_join import write_back_advisor, GUARD_PASS, GUARD_DEGRADED

logger = get_logger("report.advisor.persist")


def persist_advisor(conn, result) -> int:
    """把 1b 产出写回 recommendation_rationale。返回受影响行数（0=无操作）。

    conn 为 None / result 为 None / result 无 rationale_id / result 无句子 → 0（no-op）。
    guard 值域：result.degraded=True → degraded，否则 pass（REJECTED 不到此处——
    守卫全拒时 degraded 路径已覆盖）。
    任何异常 → 记 warning 并返回 0（渲染继续）。
    """
    if conn is None or result is None:
        return 0
    rationale_id = getattr(result, "rationale_id", None)
    if not rationale_id:
        return 0
    sentences = getattr(result, "sentences", None)
    if not sentences:
        return 0
    text = getattr(result, "text", "") or ""
    if not text.strip():
        return 0
    guard = GUARD_DEGRADED if getattr(result, "degraded", False) else GUARD_PASS
    try:
        return write_back_advisor(conn, rationale_id, text, guard)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"persist_advisor 写回失败（不阻断渲染）: "
                       f"rationale_id={rationale_id} err={exc}")
        return 0
