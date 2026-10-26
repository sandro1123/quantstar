# -*- coding: utf-8 -*-
"""接线压实测 — 守卫必须跨越对象边界（判例#150）

反打实录（2026-10-09）：把 tomorrow_picks 的 `persist_advisor(conn, res)` 注掉，
test_advisor_persist.py + test_sections.py 15 测全绿 → 证明原测试只守护函数
本身，不守护「调用点真的调了它」。本文件补这一维。

手法：真渲染 tomorrow_picks.build(context) 一遍，检查库。
不 mock persist 本身——mock persist = 守护 patch 而非守护接线。
"""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from report_layer.advisor import (AdvisorResult, AdvisorSentence, ensure_rationale_table)
from report_layer.advisor.models import RecommendationArgument
from report_layer.advisor.rationale_join import GUARD_PASS
from report_layer.sections.tomorrow_picks import TomorrowPicksSection


def _db_with_rationale(rid="rid-wire-1", code="600000", rec_date="2026-10-09",
                       reasoning="动量因子近20日强势，资金持续流入。"):
    conn = sqlite3.connect(":memory:")
    ensure_rationale_table(conn)
    conn.execute(
        "INSERT OR REPLACE INTO recommendation_rationale "
        "(rationale_id, code, rec_date, verdict, confidence, position_pct, reasoning, "
        "expert_name, key_points, invalidation_conditions, claims, trace_id, run_id, request_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rid, code, rec_date, "buy", 0.72, 0.15, reasoning, "chairman",
         '["动量强势"]', '["跌破10.5元止损"]', '[]', "t-wire", "run-w", "req-w"))
    conn.commit()
    return conn


class _DB:
    def __init__(self, conn):
        self._conn = conn

    def get_connection(self):
        return self._conn


class _DL:
    def __init__(self, db):
        self.db = db


class _Ctx:
    date = "2026-10-09"
    data_layer = None
    strategy_layer = object()
    llm_service = None


def test_render_persists_advisor_text(monkeypatch):
    """真渲染 tomorrow_picks.build() 一次 → rationale 表 advisor_text 被写上。

    这就是接线守卫：把渲染路径里的 persist_advisor 调用注掉，它必须红。
    """
    from report_layer.sections import tomorrow_picks as tp

    conn = _db_with_rationale()
    captured = {}

    def fake_rewrite(arg, llm=None, trace_db=None, date=""):
        captured["rid"] = arg.rationale_id
        return AdvisorResult(
            sentences=[AdvisorSentence(text="动量持续走强，短线可关注。",
                                       source="reasoning", trace_id="t-wire")],
            degraded=False, text="动量持续走强，短线可关注。",
            rationale_id=arg.rationale_id)

    def fake_load(code, date, c):
        return RecommendationArgument(
            code=code, verdict="buy", confidence=0.72, position_pct=0.15,
            reasoning="动量因子近20日强势，资金持续流入。",
            key_points=["动量强势"], invalidation_conditions=["跌破10.5元止损"],
            rationale_id="rid-wire-1", trace_id="t-wire")

    monkeypatch.setattr(tp, "rewrite_to_advisor", fake_rewrite)
    monkeypatch.setattr(tp, "load_argument", fake_load)
    monkeypatch.setattr(
        tp, "_candidate_rows",
        lambda c, as_of, cap=30: [("600000", "测试股", 10.0, 10.5, 9.5, 11.5, 0.83, None)])
    monkeypatch.setattr(tp, "_review_status_cn", lambda c, code: "已批准")
    monkeypatch.setattr(tp, "_health_discount", lambda c: "")

    ctx = _Ctx()
    ctx.data_layer = _DL(_DB(conn))

    section = TomorrowPicksSection()
    result = section.build(ctx)

    assert captured.get("rid") == "rid-wire-1", "rewrite_to_advisor 未被调用（接线断）"
    row = conn.execute(
        "SELECT advisor_text, advisor_guard FROM recommendation_rationale "
        "WHERE rationale_id='rid-wire-1'").fetchone()
    assert row is not None and row[0] == "动量持续走强，短线可关注。", (
        f"advisor_text 未落库（接线断）：{row!r}")
    assert row[1] == GUARD_PASS
    assert "动量持续走强" in result.body, "渲染文本里也没有（两处都断）"
    conn.close()
