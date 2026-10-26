# -*- coding: utf-8 -*-
"""件① 守卫降级落库留痕 — advisor_guard_trace 表（v9，候并窗）

审计官取证面：忠实性守卫在生产拒绝了什么、走了哪条降级腿——
从「看代码推行为」升级为「生产留痕实证」。

设计（统筹裁定）：
- 只记拒绝，不记放行（留痕面=取证异常，不是流水账，防 0 语义稀释）
- 跨层表进中央迁移清单（v3/v5/v6/v7 先例；DDL 权威定义点=本模块 GUARD_TRACE_DDL，
  execution_layer/migrations.py v9 保持一致）
- 先 PRAGMA 取证：无既有守卫留痕表可复用（report_section_trace=段级留痕，粒度不同）
- 统一时间戳 datetime('now','+8 hours')（生产 UTC+8 钟惯例，v6/v7 同）
"""
import hashlib

from common.logging import get_logger
from .fidelity import GuardVerdict

logger = get_logger("report.advisor.guard_trace")

# 三闸值域（与 fidelity.py 语义对齐，字面锁定防静默扩域）
GATE_CONCLUSION = "conclusion"
GATE_EVIDENCE = "evidence"
GATE_TRACE = "trace"

# 降级腿值域（rewrite 三层瀑布）
PATH_RULE_FALLBACK = "rule_fallback"   # LLM 被拒 → 规则腿接住
PATH_RAW = "raw"                        # 两腿皆拒 → 原文降级
PATH_NONE = "none"                      # 无降级（放行；不落库，仅防御性占位）

GUARD_TRACE_DDL = """CREATE TABLE IF NOT EXISTS advisor_guard_trace (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    code TEXT NOT NULL,
    rejected_gate TEXT NOT NULL,
    degrade_path TEXT NOT NULL,
    llm_output_hash TEXT,
    detail TEXT NOT NULL DEFAULT '',
    rationale_id TEXT,
    trace_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))
)"""

_GUARD_TRACE_IDX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_agt_date ON advisor_guard_trace (date)",
    "CREATE INDEX IF NOT EXISTS idx_agt_code ON advisor_guard_trace (code, date)",
)


def ensure_guard_trace_table(conn) -> None:
    """幂等建表+索引。conn: sqlite3.Connection。"""
    if conn is None:
        return
    conn.execute(GUARD_TRACE_DDL)
    for ddl in _GUARD_TRACE_IDX_DDL:
        conn.execute(ddl)
    conn.commit()


def record_guard_rejection(conn, date: str, code: str, guard_verdict: GuardVerdict,
                           degrade_path: str, llm_output: str = "",
                           rationale_id: str = "", trace_id: str = "") -> int:
    """守卫拒绝落痕一行。守卫放行（ok=True）不落库返回 0。
    degrade_path ∈ {rule_fallback, raw}（放行不进来，none 仅防御）。
    rationale_id/trace_id：审计回 JOIN 锚——被闸拒的某句 → advisor_guard_trace →
    recommendation_rationale.rationale_id → claims JSON → source.ref（取证链闭环）。"""
    if conn is None or guard_verdict is None or guard_verdict.ok:
        return 0
    out_hash = ""
    if llm_output:
        out_hash = hashlib.sha256(llm_output.encode("utf-8", "replace")).hexdigest()
    conn.execute(
        "INSERT INTO advisor_guard_trace (date, code, rejected_gate, degrade_path, "
        "llm_output_hash, detail, rationale_id, trace_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (date, code, guard_verdict.gate or "unknown", degrade_path or PATH_RAW,
         out_hash, guard_verdict.detail or "", rationale_id or None, trace_id or None),
    )
    conn.commit()
    return 1
