# -*- coding: utf-8 -*-
"""渲染链写连接解析 — 守卫留痕落库的跨层写入口（审计 P0③ 修复）

审计复核 P0③（2026-09-21）：stock_picks/tomorrow_picks 调 rewrite 未传 trace_db
→ conn=None → record_guard_rejection 恒返 0 → **生产渲染拒绝零留痕**（取证面静默空）。
修法：两处渲染传入可写 conn + date；禁拿只读 conn 当写用。

跨层写登记（十层契约条款2）：报告层 → 执行层库（advisor_guard_trace 表）。
写入面 = 本模块唯一入口；表 DDL 权威点 = advisor.guard_trace.GUARD_TRACE_DDL；
迁移登记 = execution_layer/migrations.py v9。
写失败一律不阻塞渲染（留痕是取证面，不是推荐主链）——由 rewrite._trace 的 try/except 承接。
"""
from common.logging import get_logger

logger = get_logger("report.advisor.trace_conn")

# context 上标记「本数据源只读」的属性名（若置真则拒绝写，防拿只读连接当写用）
_READONLY_FLAGS = ("readonly", "is_readonly", "read_only")


def _is_readonly(db) -> bool:
    for flag in _READONLY_FLAGS:
        if getattr(db, flag, False):
            return True
    return False


def resolve_trace_conn(context):
    """解析渲染链的守卫留痕写连接。取不到/只读 → None（诚实降级为不留痕，不炸渲染）。
    幂等确保表存在（生产 v9 迁移会在管道启动建表；渲染可能先于迁移跑，此处兜底建）。"""
    if context is None:
        return None
    try:
        dl = getattr(context, "data_layer", None)
        db = getattr(dl, "db", None) if dl is not None else None
        if db is None or _is_readonly(db):
            return None
        conn = db.get_connection()
        if conn is None:
            return None
        try:
            from .guard_trace import ensure_guard_trace_table
            ensure_guard_trace_table(conn)   # 幂等建表（含索引），防 no such table 静默吞
        except Exception as e:
            logger.warning(f"guard_trace 建表失败(留痕将降级为无): {e}")
        return conn
    except Exception as e:
        logger.warning(f"resolve_trace_conn 失败(留痕降级为无): {e}")
        return None
