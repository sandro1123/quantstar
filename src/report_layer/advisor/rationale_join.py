# -*- coding: utf-8 -*-
"""1a schema 接通层 — recommendation_rationale 读取/写回（REC卡 trace JOIN 收口）

W1 schema 定稿 fe45b0f3 契约（CONTRACT_FIELDS 冻结，改名须统筹批）：
  UNIQUE(code, rec_date, expert_name)；claims JSON 每句可回溯（出口硬闸载体）；
  1b 写回位 = advisor_text + advisor_guard 两列。

镜像 DDL 说明：本模块 RATIONALE_DDL_MIRROR 与 agent_layer/rationale.RATIONALE_DDL
（W1，fe45b0f3）逐字一致；W1 分支并入 main 后，ensure_rationale_table 改为转调
agent_layer.rationale（单一来源收回），此处镜像仅为本分支独立可测。
"""
from __future__ import annotations

import json
import sqlite3

from common.logging import get_logger
from .models import RecommendationArgument

logger = get_logger("report.advisor.rationale_join")

# 守卫结果值域（advisor_guard 列三态，字面锁定）
GUARD_PASS = "pass"
GUARD_DEGRADED = "degraded"
GUARD_REJECTED = "rejected"

# 与 agent_layer/rationale.RATIONALE_DDL（W1 fe45b0f3）逐字镜像；W1 并 main 后收回单一来源
RATIONALE_DDL_MIRROR = """
CREATE TABLE IF NOT EXISTS recommendation_rationale (
    rationale_id TEXT PRIMARY KEY,
    code TEXT NOT NULL,
    rec_date TEXT NOT NULL,
    -- FinalDecision 组 (decision_hub.py 产出)
    verdict TEXT,
    confidence REAL,
    position_pct REAL,
    reasoning TEXT,
    -- ExpertReport 组 (辩论六专家产出)
    expert_name TEXT,
    key_points TEXT,             -- JSON list
    invalidation_conditions TEXT, -- JSON list (条件失效声明, EX-4 下游)
    -- 出口硬闸: 每句 source 追溯 (claims JSON: [{text, source{kind,ref,value}}])
    claims TEXT,
    -- JOIN 锚 (1b 对接位)
    trace_id TEXT,
    run_id TEXT,
    request_id TEXT,
    -- 1b 加工面 (报告层二次加工 + 忠实性守卫结果)
    advisor_text TEXT,
    advisor_guard TEXT,
    created_at TEXT DEFAULT (datetime('now', '+8 hours')),
    UNIQUE (code, rec_date, expert_name)
)
"""


def ensure_rationale_table(conn) -> None:
    """幂等建表（镜像 DDL；若 agent_layer.rationale 已在则以其为准——单一来源优先）。"""
    if conn is None:
        return
    try:
        from agent_layer.rationale import RATIONALE_DDL
        ddl = RATIONALE_DDL
    except Exception:
        ddl = RATIONALE_DDL_MIRROR  # W1 分支未并时镜像可测
    conn.execute(ddl)
    conn.commit()


def load_argument_from_rationale(code: str, rec_date: str,
                                 conn) -> RecommendationArgument | None:
    """从 rationale 表读结构化论证（rationale_id/trace_id JOIN 锚拿全）。
    专家行优先 chairman（裁决官终审）；无 chairman 取稳定首行（expert_name 序）。"""
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT rationale_id, verdict, confidence, position_pct, reasoning, "
            "key_points, invalidation_conditions, claims, trace_id, run_id, request_id "
            "FROM recommendation_rationale WHERE code=? AND rec_date=? "
            "ORDER BY CASE WHEN expert_name='chairman' THEN 0 ELSE 1 END, expert_name, rowid "
            "LIMIT 1",
            (code, rec_date),
        ).fetchone()
    except sqlite3.OperationalError:
        return None  # 表不存在（未迁移/未建）→ 回落 agent_memory
    if not row:
        return None
    (rationale_id, verdict, confidence, position_pct, reasoning,
     key_points, invalidation, claims, trace_id, run_id, request_id) = row
    reasoning = (reasoning or "").strip()
    if not reasoning:
        return None
    try:
        claims_list = json.loads(claims) if claims else []
    except Exception:
        claims_list = []
    return RecommendationArgument(
        code=code,
        verdict=verdict or "avoid",       # 词表翻译仍由 source.load_argument 统一处理
        confidence=float(confidence) if confidence is not None else 0.0,
        position_pct=float(position_pct) if position_pct is not None else 0.0,
        reasoning=reasoning,
        key_points=json.loads(key_points) if key_points else [],
        invalidation_conditions=json.loads(invalidation) if invalidation else [],
        claims=claims_list,               # 1a 合法扩展域（闸2 封闭校验用它）
        factors=[],
        numbers=[],
        rationale_id=rationale_id,
        trace_id=trace_id or None,
    )


def write_back_advisor(conn, rationale_id: str, advisor_text: str,
                       advisor_guard: str) -> int:
    """写回 1b 两列（advisor_text/advisor_guard）到对应 rationale 行。
    返回受影响行数（0=rationale_id 不存在）。guard 值域三态字面锁定。

    P1② 防护（审计 2026-09-21）：1a 的 record_rationale 用 INSERT OR REPLACE
    + 每次新 uuid → REPLACE 会**清空 1b 两列并换新 rationale_id**，使既有
    advisor_guard_trace 行的 rationale_id 变成孤儿锚（反 JOIN 断链）。
    修法三件：
      (a) 写回时记 pre_image（原 advisor_text/guard），供审计对照；
      (b) 提供 find_orphan_traces()：找出 guard_trace 里指不到 rationale 行的锚；
      (c) write_back 用 UPDATE（不换 id），绝不 REPLACE。
    """
    if conn is None or not rationale_id:
        return 0
    if advisor_guard not in (GUARD_PASS, GUARD_DEGRADED, GUARD_REJECTED):
        raise ValueError(f"advisor_guard 值域外: {advisor_guard!r}")
    # (a) 捕获 pre_image（若列不存在则忽略——旧库/未迁移场景）
    pre_text = pre_guard = None
    try:
        row = conn.execute(
            "SELECT advisor_text, advisor_guard FROM recommendation_rationale "
            "WHERE rationale_id=?", (rationale_id,)).fetchone()
        if row is not None:
            pre_text = row[0] if not hasattr(row, "keys") else row["advisor_text"]
            pre_guard = row[1] if not hasattr(row, "keys") else row["advisor_guard"]
    except sqlite3.OperationalError:
        pass
    cur = conn.execute(
        "UPDATE recommendation_rationale SET advisor_text=?, advisor_guard=? "
        "WHERE rationale_id=?",
        (advisor_text, advisor_guard, rationale_id),
    )
    conn.commit()
    n = cur.rowcount
    if n == 0:
        logger.warning(f"write_back 目标 rationale_id 不存在（锚可能已被 REPLACE 换掉）: {rationale_id}")
    elif pre_text or pre_guard:
        logger.info(f"write_back 覆盖既有 1b 列: rationale_id={rationale_id} "
                    f"pre_guard={pre_guard!r} → {advisor_guard!r}")
    return n


def find_orphan_traces(conn) -> list:
    """(b) 找出 advisor_guard_trace 中 rationale_id 指不到现存 rationale 行的锚
    （P1② 后果：1a 的 REPLACE+新 uuid 会制造孤儿锚，审计反 JOIN 断链）。
    返回 [{trace_row_id, code, date, rationale_id}, ...]；表不存在返回 []。"""
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT t.id, t.code, t.date, t.rationale_id FROM advisor_guard_trace t "
            "LEFT JOIN recommendation_rationale r ON r.rationale_id = t.rationale_id "
            "WHERE t.rationale_id IS NOT NULL AND t.rationale_id <> '' AND r.rationale_id IS NULL"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for r in rows:
        if hasattr(r, "keys"):
            out.append({"trace_row_id": r["id"], "code": r["code"],
                        "date": r["date"], "rationale_id": r["rationale_id"]})
        else:
            out.append({"trace_row_id": r[0], "code": r[1], "date": r[2], "rationale_id": r[3]})
    return out
