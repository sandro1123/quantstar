# -*- coding: utf-8 -*-
"""advisor_text 写回接线 TDD — persist_advisor 把 1b 产出落库

病根：`write_back_advisor`（report_layer/advisor/rationale_join.py:114）生产零调用方，
`recommendation_rationale.advisor_text` 91/91 全空。渲染路径 `rewrite_to_advisor`
出了 result 从不落库 = 「建了没接线」又一变体。

判据族：#163 假活（呈现层≠事实层）/ 铁律#12 写库失败不阻断渲染。
"""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from report_layer.advisor.persist import persist_advisor
from report_layer.advisor.models import AdvisorResult, AdvisorSentence
from report_layer.advisor.rationale_join import (
    ensure_rationale_table, write_back_advisor, GUARD_PASS, GUARD_DEGRADED,
)


def _make_db():
    conn = sqlite3.connect(":memory:")
    ensure_rationale_table(conn)
    return conn


def _insert_rationale(conn, rid="rid-0001", code="600000", rec_date="2026-10-09",
                      reasoning="动量因子近20日强势", expert="chairman"):
    conn.execute(
        "INSERT OR REPLACE INTO recommendation_rationale "
        "(rationale_id, code, rec_date, verdict, confidence, position_pct, reasoning, "
        "expert_name, key_points, invalidation_conditions, claims, trace_id, run_id, request_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rid, code, rec_date, "buy", 0.72, 0.15, reasoning, expert,
         '["动量强势"]', '["跌破10.5元止损"]', '[]', "t123", "run1", "req1"))
    conn.commit()


def _ok_result(rid="rid-0001", text="动量持续走强，短线可关注。", degraded=False):
    return AdvisorResult(
        sentences=[AdvisorSentence(text=text, source="reasoning", trace_id="t123")],
        degraded=degraded, text=text, rationale_id=rid)


class TestPersistAdvisor:
    def test_happy_path_writes_two_cols(self):
        """正常路径：advisor_text + advisor_guard 都落库，guard=pass"""
        conn = _make_db()
        _insert_rationale(conn)
        n = persist_advisor(conn, _ok_result())
        assert n == 1
        row = conn.execute(
            "SELECT advisor_text, advisor_guard FROM recommendation_rationale "
            "WHERE rationale_id='rid-0001'").fetchone()
        assert row[0] == "动量持续走强，短线可关注。"
        assert row[1] == GUARD_PASS
        conn.close()

    def test_degraded_sets_guard_degraded(self):
        """守卫降级 → guard=degraded，文本仍落（诚实水位不是空）"""
        conn = _make_db()
        _insert_rationale(conn)
        n = persist_advisor(conn, _ok_result(degraded=True))
        assert n == 1
        row = conn.execute(
            "SELECT advisor_guard FROM recommendation_rationale WHERE rationale_id='rid-0001'"
        ).fetchone()
        assert row[0] == GUARD_DEGRADED
        conn.close()

    def test_no_rationale_id_returns_zero(self):
        """result 无 rationale_id（回落 agent_memory 场景）→ 0 不写不炸"""
        conn = _make_db()
        _insert_rationale(conn)
        res = _ok_result()
        res.rationale_id = None
        assert persist_advisor(conn, res) == 0
        row = conn.execute(
            "SELECT advisor_text FROM recommendation_rationale WHERE rationale_id='rid-0001'"
        ).fetchone()
        assert row[0] is None
        conn.close()

    def test_empty_sentences_returns_zero(self):
        """result.sentences 空（守卫全拒且无降级文本）→ 0 不写"""
        conn = _make_db()
        _insert_rationale(conn)
        res = AdvisorResult(sentences=[], degraded=False, text="", rationale_id="rid-0001")
        assert persist_advisor(conn, res) == 0
        conn.close()

    def test_none_conn_returns_zero(self):
        """拿不到连接 → 0（no-op，绝不炸渲染）"""
        assert persist_advisor(None, _ok_result()) == 0

    def test_none_result_returns_zero(self):
        """result 本身是 None → 0（no-op）"""
        conn = _make_db()
        assert persist_advisor(conn, None) == 0
        conn.close()

    def test_unknown_rationale_id_returns_zero(self):
        """目标 id 不存在（锚被 REPLACE 换掉）→ 0，由 write_back_advisor 记 warning"""
        conn = _make_db()
        assert persist_advisor(conn, _ok_result(rid="no-such-id")) == 0
        conn.close()

    def test_write_failure_does_not_raise(self):
        """写库异常绝不阻断渲染（铁律#12）：closed conn → 0 不抛"""
        conn = _make_db()
        _insert_rationale(conn)
        conn.close()
        assert persist_advisor(conn, _ok_result()) == 0

    def test_result_none_safe(self):
        """result 为 None 时不碰连接"""
        assert persist_advisor(None, None) == 0
