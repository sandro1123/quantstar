# -*- coding: utf-8 -*-
"""审计复核 P0③ 判据 — 渲染链真落痕（FakeLLM 拒回归验 guard_trace 有行）

审计实测：stock_picks/tomorrow_picks 调 rewrite 未传 trace_db → conn=None → 恒返 0
→ 生产渲染拒绝零留痕。修法=传可写 conn+date。本测试直接构造 context 渲染一次，
断言 guard_trace 真出 1 行（不是看代码，是看落库）。
"""
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from report_layer.advisor.guard_trace import GUARD_TRACE_DDL, GATE_EVIDENCE


class FakeRejectingLLM:
    """产出编造依据（历史战绩/胜率）→ 必被闸2 拒"""

    def call(self, prompt, **kw):
        return "该股基本面向好，历史战绩显示策略胜率八成，建议重仓买入"


def _make_db():
    conn = sqlite3.connect(":memory:")
    conn.execute(GUARD_TRACE_DDL)
    conn.execute("CREATE TABLE agent_memory (code TEXT, date TEXT, verdict TEXT, "
                 "confidence REAL, reasoning TEXT, created_at TEXT)")
    conn.execute("INSERT INTO agent_memory VALUES ('600000','2026-09-22','推荐',0.72,"
                 "'动量因子近20日强势，综合判断可买入','2026-09-22 16:12:00')")
    conn.execute("CREATE TABLE recommendation_rationale (rationale_id TEXT PRIMARY KEY, "
                 "code TEXT, rec_date TEXT, verdict TEXT, confidence REAL, position_pct REAL, "
                 "reasoning TEXT, expert_name TEXT, key_points TEXT, invalidation_conditions TEXT, "
                 "claims TEXT, trace_id TEXT, run_id TEXT, request_id TEXT, "
                 "advisor_text TEXT, advisor_guard TEXT)")
    conn.commit()
    return conn


def _ctx(conn, llm):
    class FakeDB:
        def get_connection(self):
            return conn

    class FakeStrategy:
        def run_daily(self, date):
            return SimpleNamespace(signals=[SimpleNamespace(code="600000", action="buy", strength=0.8)],
                                   regime="")

    return SimpleNamespace(date="2026-09-22", data_layer=SimpleNamespace(db=FakeDB()),
                           strategy_layer=FakeStrategy(), llm_service=llm, _cache={})


class TestStockPicksTraceWiring:
    def test_rejection_lands_in_guard_trace(self):
        """精选节渲染（LLM 编造被拒）→ guard_trace 真有行（审计 P0③ 原案）"""
        from report_layer.sections.stock_picks import StockPicksSection
        conn = _make_db()
        ctx = _ctx(conn, FakeRejectingLLM())
        result = StockPicksSection().build(ctx)
        assert result is not None
        rows = conn.execute("SELECT code, rejected_gate, degrade_path FROM advisor_guard_trace"
                            ).fetchall()
        assert len(rows) == 1, f"应恰好 1 行拒绝留痕，实得 {len(rows)}"
        assert rows[0][0] == "600000"
        assert rows[0][1] == GATE_EVIDENCE
        assert rows[0][2] == "rule_fallback"

    def test_no_rejection_no_trace_row(self):
        """守卫放行 → 零留痕（不记流水账）"""
        from report_layer.sections.stock_picks import StockPicksSection
        conn = _make_db()
        ctx = _ctx(conn, None)  # 无 LLM → 规则腿，忠实产出
        StockPicksSection().build(ctx)
        assert conn.execute("SELECT COUNT(*) FROM advisor_guard_trace").fetchone()[0] == 0


class TestTomorrowPicksTraceWiring:
    def test_rejection_lands_in_guard_trace(self):
        from report_layer.sections.tomorrow_picks import TomorrowPicksSection
        conn = _make_db()
        conn.execute("CREATE TABLE stock_pool (code TEXT, name TEXT, entry_low REAL, "
                     "entry_high REAL, stop_loss REAL, take_profit REAL, added_reason TEXT, "
                     "status TEXT, valid_until TEXT, initial_score REAL)")
        conn.execute("INSERT INTO stock_pool VALUES ('600000','浦发银行',10.0,10.5,9.5,12.0,"
                     "NULL,'watch',NULL,0.9)")
        conn.commit()
        ctx = _ctx(conn, FakeRejectingLLM())
        result = TomorrowPicksSection().build(ctx)
        assert result is not None
        rows = conn.execute("SELECT code, rejected_gate FROM advisor_guard_trace").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "600000"
        assert rows[0][1] == GATE_EVIDENCE


class TestTraceConnResolution:
    def test_readonly_flag_refuses_write_conn(self):
        """审计要求：禁拿只读 conn 当写用"""
        from report_layer.advisor.trace_conn import resolve_trace_conn

        class ReadonlyDB:
            readonly = True

            def get_connection(self):
                raise AssertionError("只读源不应被取得写连接")

        ctx = SimpleNamespace(data_layer=SimpleNamespace(db=ReadonlyDB()))
        assert resolve_trace_conn(ctx) is None

    def test_missing_data_layer_safe(self):
        from report_layer.advisor.trace_conn import resolve_trace_conn
        assert resolve_trace_conn(SimpleNamespace(data_layer=None)) is None
        assert resolve_trace_conn(None) is None
