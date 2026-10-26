# -*- coding: utf-8 -*-
"""REC 模块1a (2026-09-21): recommendation_rationale 结构化论证 schema。

接口契约 (统筹 09-21 三点):
1. schema 与 6f73b79e (1b) 的 trace_id+rationale_id 字段位对齐, 字段名定稿
   不再改 (改=两会话返工);
2. 覆盖 FinalDecision{verdict,confidence,position_pct,reasoning} +
   ExpertReport{key_points,invalidation_conditions} 两组字段语义;
3. 「每句 source 可 JOIN 追溯到 LLM run_id」是出口硬闸 (讨论#4 问题2 用户
   定谳: 推荐理由每个数字须能 JOIN 到 source field) — claims JSON 每句自带
   source {kind, ref, value}, ref 可 JOIN agent_predictions.request_id /
   pipeline_runs.run_id / factor_daily 等真 id 体系。

追溯锚普查 (2026-09-21): agent_predictions.request_id (uuid8, 2162 行) /
agent_checkpoints.trace_id (2147 行) / pipeline_runs.run_id (4027 行)。
"""
import json
import sqlite3

import pytest

from common.database import Database


# 判例#8: RED 须 AssertionError 非 ImportError。
def test_rationale_module_exists():
    try:
        import agent_layer.rationale  # noqa: F401
    except ModuleNotFoundError as e:
        raise AssertionError(f"agent_layer.rationale 未建: {e}") from e


# ---- DDL/迁移 ----

def test_rationale_table_created():
    """rationale 表可建 (幂等) 且列齐 (契约字段)."""
    from agent_layer.rationale import RATIONALE_DDL
    db = Database(":memory:")
    conn = db.get_connection()
    conn.executescript(RATIONALE_DDL)
    conn.commit()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(recommendation_rationale)").fetchall()}
    required = {
        "rationale_id", "code", "rec_date", "verdict", "confidence",
        "position_pct", "reasoning",           # FinalDecision 组
        "expert_name", "key_points", "invalidation_conditions",  # ExpertReport 组
        "claims",                              # 每句 source 追溯 (硬闸载体)
        "trace_id", "run_id", "request_id",    # JOIN 锚 (1b 对接位)
        "advisor_text", "advisor_guard",       # 1b 加工 + 忠实性守卫结果
        "created_at",
    }
    missing = required - cols
    assert not missing, f"缺契约字段: {missing} (1b 按此 JOIN, 字段名定稿不改)"


def test_claims_json_schema():
    """claims 每句自带 source{kind,ref,value} — 出口硬闸的载体."""
    from agent_layer.rationale import RATIONALE_DDL
    db = Database(":memory:")
    conn = db.get_connection()
    conn.executescript(RATIONALE_DDL)
    claims = [{"text": "量价齐升", "source": {"kind": "factor", "ref": "factor_daily.turnover_rate", "value": 5.2}}]
    conn.execute(
        "INSERT INTO recommendation_rationale (rationale_id, code, rec_date, claims) VALUES (?,?,?,?)",
        ("r1", "600001", "2026-09-21", json.dumps(claims, ensure_ascii=False)))
    conn.commit()
    back = json.loads(conn.execute(
        "SELECT claims FROM recommendation_rationale WHERE rationale_id='r1'").fetchone()[0])
    assert back[0]["source"]["kind"] == "factor"
    assert "factor_daily" in back[0]["source"]["ref"]


# ---- 写入 API (1a 施工人用) ----

def test_record_rationale_writes_row():
    """record_rationale: 从 FinalDecision+ExpertReport 组装落库."""
    from agent_layer.debate.models import FinalDecision, ExpertReport
    from agent_layer.rationale import record_rationale
    db = Database(":memory:")
    conn = db.get_connection()
    from agent_layer.rationale import RATIONALE_DDL
    conn.executescript(RATIONALE_DDL)
    conn.commit()
    fd = FinalDecision(verdict="推荐", confidence=0.8, position_pct=15.0,
                       reasoning="量价齐升+龙头地位")
    er = ExpertReport(expert_name="技术分析", score=85,
                      key_points=["放量突破"], confidence=0.7,
                      invalidation_conditions=["跌破9.5元止损线"])
    rid = record_rationale(conn, code="600001", rec_date="2026-09-21",
                           decision=fd, expert=er,
                           claims=[{"text": "放量突破",
                                    "source": {"kind": "llm", "ref": "agent_predictions.req123", "value": None}}],
                           trace_id="tr1", run_id="run1", request_id="req123")
    row = conn.execute("SELECT * FROM recommendation_rationale WHERE rationale_id=?",
                       (rid,)).fetchone()
    assert row is not None, "record_rationale 未落行"
    assert row["verdict"] == "推荐" and abs(row["position_pct"] - 15.0) < 1e-6
    assert row["expert_name"] == "技术分析"
    assert json.loads(row["key_points"]) == ["放量突破"]
    assert json.loads(row["invalidation_conditions"]) == ["跌破9.5元止损线"]
    assert row["trace_id"] == "tr1" and row["request_id"] == "req123"


def test_record_rationale_idempotent():
    """同 (code, rec_date, expert_name) 重写 = REPLACE (幂等, 不堆行)."""
    from agent_layer.debate.models import FinalDecision, ExpertReport
    from agent_layer.rationale import record_rationale, RATIONALE_DDL
    db = Database(":memory:")
    conn = db.get_connection()
    conn.executescript(RATIONALE_DDL)
    conn.commit()
    fd = FinalDecision(verdict="推荐", confidence=0.8, position_pct=15.0, reasoning="x")
    for _ in range(2):
        record_rationale(conn, code="600001", rec_date="2026-09-21",
                         decision=fd,
                         expert=ExpertReport(expert_name="技术", score=80),
                         claims=[], trace_id="t", run_id="r", request_id="q")
    n = conn.execute("SELECT COUNT(*) FROM recommendation_rationale WHERE code='600001'").fetchone()[0]
    assert n == 1, f"同键重写应 REPLACE, 实得 {n} 行"


# ---- 契约锁 (字段名定稿, 改名即 RED — 1b/1a 两会话共用) ----

def test_contract_fields_frozen():
    """契约冻结: 1b 依赖的字段名清单不得漂移 (统筹三点之 2)."""
    from agent_layer.rationale import CONTRACT_FIELDS
    frozen = {"rationale_id", "code", "rec_date", "expert_name", "trace_id", "run_id", "request_id",
              "claims", "advisor_text", "advisor_guard",
              "verdict", "confidence", "position_pct", "reasoning",
              "key_points", "invalidation_conditions"}
    assert frozen == set(CONTRACT_FIELDS), \
        f"契约字段集漂移: {frozen ^ set(CONTRACT_FIELDS)} (1b 按此 JOIN, 改名须统筹批)"


def test_decision_hub_emits_rationale_id():
    """决策枢纽产 FinalDecision 时携带 rationale_id — 接通 stock_pool.added_reason 的锚."""
    from agent_layer.debate.models import FinalDecision
    import inspect
    src = inspect.getsource(FinalDecision)
    assert "rationale_id" in src, \
        "FinalDecision 须有 rationale_id 字段 (接通 added_reason 的锚, 模块1 接通面)"
