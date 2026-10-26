# -*- coding: utf-8 -*-
"""agent_layer/rationale.py — REC 模块1a: recommendation_rationale 结构化论证。

用户定谳 (REC 卡最高指导原则): QS 是股票推荐系统; 「理」= LLM 明确判断 +
理由 + 依据, 结构化可追溯。本模块是 1a (智能体层产出) 的 schema 定稿 + 写入
API; 1b (报告层二次加工) 按 CONTRACT_FIELDS 做 JOIN, 字段名定稿不再改
(统筹 09-21: 改=两会话返工)。

出口硬闸 (讨论#4 问题2 用户定谳): 推荐理由每个数字须能 JOIN 到 source
field —— claims JSON 每句 {text, source{kind, ref, value}}, ref 指向真 id
体系: agent_predictions.request_id / pipeline_runs.run_id / factor_daily.<col>
/ news_intel.<id> 等。reasoning 文本只是摘要, 追溯面在 claims。

追溯锚普查 (2026-09-21): agent_predictions.request_id (uuid8, 2162 行) /
agent_checkpoints.trace_id (2147 行) / pipeline_runs.run_id (4027 行)。
"""
from __future__ import annotations

import json
import sqlite3
import uuid

from common.logging import get_logger

logger = get_logger("agent.rationale")

RATIONALE_DDL = """
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

# 契约冻结 (统筹 09-21 三点之 2): 1b 按 trace_id+rationale_id JOIN, 此清单
# 即两会话接口; 改名须统筹批准 (test_contract_fields_frozen 锁)。
CONTRACT_FIELDS = (
    "rationale_id", "code", "rec_date",
    "verdict", "confidence", "position_pct", "reasoning",
    "expert_name", "key_points", "invalidation_conditions",
    "claims", "trace_id", "run_id", "request_id",
    "advisor_text", "advisor_guard",
)


def _json(v) -> str:
    return json.dumps(v, ensure_ascii=False)


def record_rationale(
    conn: sqlite3.Connection,
    code: str,
    rec_date: str,
    decision,
    expert,
    claims: list[dict] | None = None,
    trace_id: str = "",
    run_id: str = "",
    request_id: str = "",
) -> str:
    """从 FinalDecision + ExpertReport 组装一行落库, 返回 rationale_id。

    幂等: UNIQUE(code, rec_date, expert_name) + INSERT OR REPLACE —
    同日同股同专家重写不堆行。
    """
    conn.execute(RATIONALE_DDL)
    rid = uuid.uuid4().hex[:12]
    conn.execute(
        """INSERT OR REPLACE INTO recommendation_rationale
        (rationale_id, code, rec_date, verdict, confidence, position_pct,
         reasoning, expert_name, key_points, invalidation_conditions,
         claims, trace_id, run_id, request_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            rid, code, rec_date,
            getattr(decision, "verdict", None),
            getattr(decision, "confidence", None),
            getattr(decision, "position_pct", None),
            getattr(decision, "reasoning", None),
            getattr(expert, "expert_name", None),
            _json(getattr(expert, "key_points", [])),
            _json(getattr(expert, "invalidation_conditions", [])),
            _json(claims or []),
            trace_id, run_id, request_id,
        ),
    )
    return rid
