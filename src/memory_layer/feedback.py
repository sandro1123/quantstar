"""agent_evaluate 结果回流 → feedback 记忆 (Phase 1 唯一自动写入源)

接入点: ops_layer/orchestrator.py::_run_agent_evaluate (ev.evaluate 返回后)。
诚实降级: 写入失败只 warning, 绝不阻断管道。
"""
from __future__ import annotations

from common.logging import get_logger

logger = get_logger("memory.feedback")


def record_evaluate_feedback(db, date: str, meta: dict) -> int | None:
    """把一次 agent_evaluate 的命中率结算写成 agent:evaluator 的 feedback 记忆。

    evaluated==0 (无数据) 不写 — 无数据不造记忆。
    """
    try:
        evaluated = int(meta.get("evaluated") or 0)
        if evaluated <= 0:
            return None
        hits = int(meta.get("hits") or 0)
        hit_rate = hits / evaluated
        bt_hit_rate = meta.get("bt_hit_rate")
        content = (f"{date} 预测命中率 {hit_rate:.0%} ({hits}/{evaluated}), "
                   f"回测口径 {bt_hit_rate}")
        evidence = (f"agent_evaluate:{date}:evaluated={evaluated},hits={hits},"
                    f"bt_hit_rate={bt_hit_rate}")
        from memory_layer.store import MemoryStore
        return MemoryStore(db).write("agent:evaluator", "feedback", content,
                                     evidence=evidence, confidence=min(1.0, evaluated / 20))
    except Exception as e:
        logger.warning(f"record_evaluate_feedback 失败 (诚实降级, 不阻断): {e}")
        return None
