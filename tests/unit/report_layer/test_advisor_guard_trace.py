# -*- coding: utf-8 -*-
"""件①守卫 degraded 落库留痕 TDD — advisor_guard_trace 表（v9）

审计官取证面：哪天/哪票/被哪道闸拒/降级走了哪条腿/原始LLM产出hash。
统一时间戳 = datetime('now','+8 hours')（生产 UTC+8 钟惯例）。
"""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from report_layer.advisor.guard_trace import (
    ensure_guard_trace_table, record_guard_rejection, GATE_CONCLUSION,
    GATE_EVIDENCE, GATE_TRACE,
)
from report_layer.advisor.models import RecommendationArgument, AdvisorSentence
from report_layer.advisor.fidelity import FidelityGuard


@pytest.fixture()
def db():
    conn = sqlite3.connect(":memory:")
    yield conn
    conn.close()


class TestEnsureTable:
    def test_creates_table_idempotent(self, db):
        ensure_guard_trace_table(db)
        ensure_guard_trace_table(db)  # 幂等
        cols = [r[1] for r in db.execute("PRAGMA table_info(advisor_guard_trace)")]
        for c in ("id", "date", "code", "rejected_gate", "degrade_path",
                  "llm_output_hash", "detail", "created_at"):
            assert c in cols, f"缺列 {c}"

    def test_indexes_exist(self, db):
        ensure_guard_trace_table(db)
        names = [r[1] for r in db.execute("PRAGMA index_list(advisor_guard_trace)")]
        assert any("date" in n for n in names)


class TestRecordRejection:
    def _arg(self):
        return RecommendationArgument(
            code="600000", verdict="buy", confidence=0.72, position_pct=0.15,
            reasoning="动量因子近20日强势，综合判断可买入",
            factors=["动量"], numbers=["20日"])

    def test_records_full_evidence(self, db):
        ensure_guard_trace_table(db)
        arg = self._arg()
        bad = [AdvisorSentence(text="历史战绩显示胜率八成", source="reasoning")]
        v = FidelityGuard(arg).check(bad)
        assert v.ok is False
        n = record_guard_rejection(db, date="2026-09-21", code="600000",
                                   guard_verdict=v, degrade_path="rule_fallback",
                                   llm_output="历史战绩显示胜率八成，建议重仓买入")
        assert n == 1
        row = db.execute(
            "SELECT date, code, rejected_gate, degrade_path, llm_output_hash, detail "
            "FROM advisor_guard_trace").fetchone()
        assert row[0] == "2026-09-21"
        assert row[1] == "600000"
        assert row[2] == GATE_EVIDENCE
        assert row[3] == "rule_fallback"
        assert row[4] and len(row[4]) == 64      # sha256 hex
        assert "历史战绩" in row[5]

    def test_all_three_gate_values(self, db):
        """三闸值域锁定：conclusion/evidence/trace（防 gate 字段静默扩域）"""
        ensure_guard_trace_table(db)
        arg = self._arg()
        # 闸1
        v1 = FidelityGuard(arg).check([AdvisorSentence(text="当前不宜买入", source="reasoning")])
        assert v1.gate == GATE_CONCLUSION
        # 闸3
        v3 = FidelityGuard(arg).check([AdvisorSentence(text="文本", source=None)])
        assert v3.gate == GATE_TRACE
        for v in (v1, v3):
            record_guard_rejection(db, date="2026-09-21", code="600000",
                                   guard_verdict=v, degrade_path="raw")
        gates = [r[0] for r in db.execute("SELECT rejected_gate FROM advisor_guard_trace")]
        assert GATE_CONCLUSION in gates and GATE_TRACE in gates

    def test_ok_verdict_not_recorded(self, db):
        """守卫放行不落痕（只记拒绝——留痕面是取证异常，不是流水账）"""
        ensure_guard_trace_table(db)
        arg = self._arg()
        v = FidelityGuard(arg).check([AdvisorSentence(text="该股动量强势", source="reasoning")])
        assert v.ok is True
        n = record_guard_rejection(db, date="2026-09-21", code="600000",
                                   guard_verdict=v, degrade_path="none")
        assert n == 0
        assert db.execute("SELECT COUNT(*) FROM advisor_guard_trace").fetchone()[0] == 0


class TestRewriteIntegration:
    def test_rewrite_wires_trace_on_llm_rejection(self, db):
        """rewrite 主入口带 db 时：LLM 被守卫拒 → 自动留痕（rule_fallback）"""
        from report_layer.advisor.rewrite import rewrite_to_advisor
        ensure_guard_trace_table(db)
        arg = RecommendationArgument(
            code="600000", verdict="buy", confidence=0.72, position_pct=0.15,
            reasoning="动量因子近20日强势，综合判断可买入",
            factors=["动量"], numbers=["20日"])

        class FakeLLM:
            def call(self, prompt, **kw):
                return "该股基本面向好，历史战绩显示策略胜率八成，建议重仓买入"

        result = rewrite_to_advisor(arg, llm=FakeLLM(), trace_db=db, date="2026-09-21")
        assert result.degraded is True
        row = db.execute("SELECT code, rejected_gate, degrade_path FROM advisor_guard_trace"
                         ).fetchone()
        assert row[0] == "600000"
        assert row[1] == GATE_EVIDENCE
        assert row[2] == "rule_fallback"

    def test_rewrite_no_trace_when_pass(self, db):
        ensure_guard_trace_table(db)
        arg = RecommendationArgument(
            code="600000", verdict="buy", confidence=0.72, position_pct=0.15,
            reasoning="动量因子近20日强势，综合判断可买入",
            factors=["动量"], numbers=["20日"])
        from report_layer.advisor.rewrite import rewrite_to_advisor
        result = rewrite_to_advisor(arg, llm=None, trace_db=db, date="2026-09-21")
        assert result.degraded is False
        assert db.execute("SELECT COUNT(*) FROM advisor_guard_trace").fetchone()[0] == 0
