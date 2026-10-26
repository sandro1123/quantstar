# -*- coding: utf-8 -*-
"""1a schema 接通 TDD — trace JOIN + advisor_text/advisor_guard 写回

W1 schema 定稿 fe45b0f3：recommendation_rationale 表，UNIQUE(code, rec_date, expert_name)，
CONTRACT_FIELDS 契约冻结（改名须统筹批）。1b 侧：
  - load_argument 优先读 rationale 表（结构化原料+rationale_id/trace_id 拿全），
    回落 agent_memory（过渡原料）
  - write_back_advisor 写回 advisor_text + advisor_guard 两列
  - RATIONALE_DDL 镜像 W1（1a 并 main 后单一来源收回 agent_layer.rationale，此处镜像注释锚）
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from report_layer.advisor.rationale_join import (
    ensure_rationale_table, load_argument_from_rationale, write_back_advisor,
    GUARD_PASS, GUARD_DEGRADED, GUARD_REJECTED,
)
from report_layer.advisor.models import RecommendationArgument

DDL_MIRROR_ANCHOR = "UNIQUE (code, rec_date, expert_name)"


@pytest.fixture()
def db():
    conn = sqlite3.connect(":memory:")
    yield conn
    conn.close()


def _insert_rationale(db, code="600000", rec_date="2026-09-21", verdict="buy",
                      confidence=0.72, reasoning="动量因子近20日强势",
                      rid="aabbccdd1122", expert="chairman", trace_id="t123"):
    claims = [{"text": reasoning, "source": {"kind": "factor", "ref": "factor_daily.momentum_20d", "value": "0.8"}}]
    db.execute(
        "INSERT OR REPLACE INTO recommendation_rationale "
        "(rationale_id, code, rec_date, verdict, confidence, position_pct, reasoning, "
        "expert_name, key_points, invalidation_conditions, claims, trace_id, run_id, request_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rid, code, rec_date, verdict, confidence, 0.15, reasoning, expert,
         json.dumps(["动量强势"], ensure_ascii=False),
         json.dumps(["跌破10.5元止损"], ensure_ascii=False),
         json.dumps(claims, ensure_ascii=False), trace_id, "run1", "req1"))
    db.commit()


class TestTableEnsure:
    def test_ddl_matches_w1_contract(self, db):
        """镜像 DDL 必须含 W1 契约唯一键与 1b 两列（防镜像漂移）"""
        ensure_rationale_table(db)
        cols = [r[1] for r in db.execute("PRAGMA table_info(recommendation_rationale)")]
        for c in ("rationale_id", "code", "rec_date", "verdict", "confidence",
                  "position_pct", "reasoning", "expert_name", "key_points",
                  "invalidation_conditions", "claims", "trace_id", "run_id",
                  "request_id", "advisor_text", "advisor_guard"):
            assert c in cols, f"缺契约列 {c}"
        sql = db.execute("SELECT sql FROM sqlite_master WHERE name='recommendation_rationale'"
                         ).fetchone()[0]
        assert DDL_MIRROR_ANCHOR in sql


class TestLoadFromRationale:
    def test_loads_structured_with_trace_anchors(self, db):
        """优先级1: rationale 表行 → argument 带 rationale_id/trace_id（JOIN 锚拿全）"""
        ensure_rationale_table(db)
        _insert_rationale(db)
        arg = load_argument_from_rationale("600000", "2026-09-21", db)
        assert arg is not None
        assert arg.rationale_id == "aabbccdd1122"
        assert arg.trace_id == "t123"
        assert arg.verdict == "buy"
        assert arg.key_points == ["动量强势"]
        assert arg.invalidation_conditions == ["跌破10.5元止损"]

    def test_missing_returns_none(self, db):
        ensure_rationale_table(db)
        assert load_argument_from_rationale("999999", "2026-09-21", db) is None

    def test_expert_preference_chairman_first(self, db):
        """多专家行时取 chairman（裁决官终审），无 chairman 取第一行（稳定序）"""
        ensure_rationale_table(db)
        _insert_rationale(db, rid="r1", expert="tech")
        _insert_rationale(db, rid="r2", expert="chairman")
        arg = load_argument_from_rationale("600000", "2026-09-21", db)
        assert arg.rationale_id == "r2"


class TestWriteBack:
    def test_write_back_two_columns(self, db):
        """写回 advisor_text + advisor_guard 到对应行（UNIQUE 定位）"""
        ensure_rationale_table(db)
        _insert_rationale(db)
        n = write_back_advisor(db, rationale_id="aabbccdd1122",
                               advisor_text="该股动量强势，值得关注",
                               advisor_guard=GUARD_PASS)
        assert n == 1
        row = db.execute("SELECT advisor_text, advisor_guard FROM recommendation_rationale "
                         "WHERE rationale_id='aabbccdd1122'").fetchone()
        assert row[0] == "该股动量强势，值得关注"
        assert row[1] == GUARD_PASS

    def test_guard_states_enum(self, db):
        """守卫结果值域：pass/degraded/rejected 三态锁定"""
        assert (GUARD_PASS, GUARD_DEGRADED, GUARD_REJECTED) == ("pass", "degraded", "rejected")

    def test_write_back_unknown_id_zero(self, db):
        ensure_rationale_table(db)
        assert write_back_advisor(db, rationale_id="nope", advisor_text="x",
                                  advisor_guard=GUARD_PASS) == 0


class TestSourceIntegration:
    def test_load_argument_prefers_rationale_over_memory(self, db):
        """load_argument 优先 rationale 表，回落 agent_memory（两库同 conn 场景）"""
        from report_layer.advisor.source import load_argument
        ensure_rationale_table(db)
        db.execute("CREATE TABLE agent_memory (code TEXT, date TEXT, verdict TEXT, "
                   "confidence REAL, reasoning TEXT, created_at TEXT)")
        db.execute("INSERT INTO agent_memory VALUES ('600000','2026-09-21','观望',0.3,"
                   "'memory旧叙述','x')")
        _insert_rationale(db)
        arg = load_argument("600000", "2026-09-21", db)
        assert arg.rationale_id == "aabbccdd1122"        # 来自 rationale 表
        assert "动量因子近20日强势" in arg.reasoning      # rationale 原料，非 memory 兜底
