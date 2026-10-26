"""agent_evaluate → memory_layer feedback 接线测试 (orchestrator 回流)"""
from unittest.mock import MagicMock

from common.database import Database
from execution_layer.migrations import ensure_schema
from memory_layer.store import MemoryStore
from ops_layer.orchestrator import PipelineOrchestrator


def _fake_env(monkeypatch, db):
    app = MagicMock()
    app.agent = MagicMock()
    app.data.db = db
    monkeypatch.setattr("bootstrap.bootstrap_app", lambda: app)

    bt = MagicMock()
    bt.find_evaluable_date.return_value = "2026-08-28"
    bt.evaluate.return_value = MagicMock(
        forward_returns={"000001": 0.05}, hit_rate=0.6,
        evaluated=10, total_predictions=12, unevaluated=2)
    monkeypatch.setattr("backtest_layer.evaluation.agent_eval.AgentEvaluator", lambda d: bt)

    ev = MagicMock()
    ev.evaluate.return_value = {"evaluated": 10, "hits": 6, "misses": 4, "skipped": 0}
    monkeypatch.setattr("agent_layer.quality.evaluator.Evaluator", lambda database=None: ev)


def test_agent_evaluate_writes_feedback_memory(monkeypatch):
    db = Database(":memory:")
    ensure_schema(db)
    _fake_env(monkeypatch, db)
    orch = PipelineOrchestrator(db, MagicMock(), MagicMock())
    result = orch._run_agent_evaluate({})
    assert result.success
    mems = MemoryStore(db).read(scope="agent:evaluator", kind="feedback")
    assert len(mems) == 1
    assert "60%" in mems[0].content
    assert mems[0].evidence.startswith("agent_evaluate:2026-08-28:")


def test_agent_evaluate_no_memory_when_zero_evaluated(monkeypatch):
    db = Database(":memory:")
    ensure_schema(db)
    _fake_env(monkeypatch, db)
    import agent_layer.quality.evaluator as evmod
    ev = MagicMock()
    ev.evaluate.return_value = {"evaluated": 0, "hits": 0, "misses": 0, "skipped": 3}
    monkeypatch.setattr(evmod, "Evaluator", lambda database=None: ev)
    orch = PipelineOrchestrator(db, MagicMock(), MagicMock())
    result = orch._run_agent_evaluate({})
    assert result.success
    assert MemoryStore(db).read(scope="agent:evaluator") == []
