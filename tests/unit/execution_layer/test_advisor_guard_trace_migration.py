# -*- coding: utf-8 -*-
"""审计复核 P0② 判据 — v9 建表列集断言

审计实测：v9 DDL 缺 rationale_id/trace_id（d1c0336c message 称「逐字一致」失实）。
后果链：生产 v8→v9 升级后 INSERT 抛 no such column → 被 rewrite._trace 吞成 warning
→ 取证面静默丢失。
修法：v9 未并 main 无存量 → 直接改 v9 CREATE 带两列；并加列集断言锁死
（DDL 权威点 guard_trace.GUARD_TRACE_DDL 与中央清单 MIGRATIONS v9 必须同列集）。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from common.database import Database
from execution_layer.migrations import MIGRATIONS, TARGET_VERSION, ensure_schema
from report_layer.advisor.guard_trace import GUARD_TRACE_DDL

# 1b 守卫留痕表契约列集（审计取证链两锚列是硬要求）
REQUIRED_COLUMNS = {
    "id", "date", "code", "rejected_gate", "degrade_path",
    "llm_output_hash", "detail", "rationale_id", "trace_id", "created_at",
}


def _v9_statements():
    for version, statements in MIGRATIONS:
        if version == 9:
            return statements
    return []


class TestV9Columns:
    def test_v9_is_target_version(self):
        assert TARGET_VERSION >= 9

    def test_v9_ddl_has_anchor_columns(self):
        """v9 CREATE 必须带两锚列（审计 P0② 原案）"""
        ddl = next(s for s in _v9_statements() if "advisor_guard_trace" in s and "CREATE TABLE" in s)
        assert "rationale_id" in ddl, "v9 缺 rationale_id（审计取证链断）"
        assert "trace_id" in ddl, "v9 缺 trace_id（审计取证链断）"

    def test_v9_matches_authoritative_ddl_column_set(self):
        """中央清单 v9 列集 == guard_trace.GUARD_TRACE_DDL 列集（防镜像漂移）"""
        def cols_of(ddl):
            body = ddl.split("(", 1)[1].rsplit(")", 1)[0]
            out = set()
            for line in body.splitlines():
                line = line.strip().rstrip(",")
                if not line or line.startswith("--") or line.upper().startswith(("PRIMARY", "UNIQUE", "FOREIGN", "CHECK")):
                    continue
                out.add(line.split()[0])
            return out

        v9 = next(s for s in _v9_statements() if "CREATE TABLE" in s and "advisor_guard_trace" in s)
        assert cols_of(v9) == cols_of(GUARD_TRACE_DDL) == REQUIRED_COLUMNS


class TestV9AppliedOnProductionShape:
    def test_fresh_db_gets_all_columns(self):
        """实打：全新库跑 ensure_schema 后两锚列真存在（防「有约束≠会拒数据」同族）"""
        db = Database(":memory:")
        db.init_db()
        ensure_schema(db)
        cols = {r[1] for r in db.get_connection().execute("PRAGMA table_info(advisor_guard_trace)")}
        assert REQUIRED_COLUMNS <= cols, f"缺列: {REQUIRED_COLUMNS - cols}"
        db.close()

    def test_insert_with_anchors_succeeds(self):
        """实打：带两锚列的 INSERT 真能落（审计后果链的正面验证）"""
        db = Database(":memory:")
        db.init_db()
        ensure_schema(db)
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO advisor_guard_trace (date, code, rejected_gate, degrade_path, "
            "detail, rationale_id, trace_id) VALUES ('2026-09-22','600000','evidence','rule_fallback','x','rid1','tid1')"
        )
        conn.commit()
        row = conn.execute("SELECT rationale_id, trace_id FROM advisor_guard_trace").fetchone()
        # Database.get_connection 用 sqlite3.Row factory → 具名取值（判例：Row factory 下勿当元组比）
        assert (row["rationale_id"], row["trace_id"]) == ("rid1", "tid1")
        db.close()
