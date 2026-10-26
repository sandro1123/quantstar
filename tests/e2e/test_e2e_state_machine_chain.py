"""E2E: StateMachine → AgentLayer → BacktestEngine full chain"""
import pytest
from unittest.mock import MagicMock, patch
from common.database import Database
from agent_layer.core.state_machine import StateMachine, AgentState
from agent_layer import AgentLayer
from agent_layer.core.models import AgentRequest, AgentResponse, QualityMetrics


DATE = "2026-07-18"


class TestE2EStateMachineChain:
    def test_state_machine_full_lifecycle(self):
        db = Database(":memory:")
        db.init_db()
        sm = StateMachine(db)
        sm._ensure_table()

        key = sm.create("test_agent", "test_task", "trace_001")
        cp = sm.get(key)
        assert cp is not None
        assert cp.state == AgentState.PENDING
        assert cp.agent_name == "test_agent"
        assert cp.task == "test_task"

        ok = sm.transition(key, AgentState.RUNNING, "start")
        assert ok is True
        cp = sm.get(key)
        assert cp.state == AgentState.RUNNING
        assert len(cp.transitions) == 1
        assert cp.transitions[0].from_state == AgentState.PENDING
        assert cp.transitions[0].to_state == AgentState.RUNNING

        ok = sm.transition(key, AgentState.COMPLETED, "done")
        assert ok is True
        cp = sm.get(key)
        assert cp.state == AgentState.COMPLETED
        assert len(cp.transitions) == 2

        sm.set_result(key, {"verdict": "推荐", "confidence": 0.85})
        cp = sm.get(key)
        assert cp.result == {"verdict": "推荐", "confidence": 0.85}

        conn = db.get_connection()
        row = conn.execute(
            "SELECT checkpoint_key, state, result_json FROM agent_checkpoints WHERE checkpoint_key = ?",
            (key,),
        ).fetchone()
        assert row is not None
        assert row["checkpoint_key"] == key
        assert row["state"] == "completed"

    def test_state_machine_persistence(self):
        db = Database(":memory:")
        db.init_db()
        sm = StateMachine(db)
        sm._ensure_table()

        key = sm.create("persist_agent", "persist_task", "trace_002")
        sm.transition(key, AgentState.RUNNING, "start")
        sm.transition(key, AgentState.COMPLETED, "done")
        sm.set_result(key, {"verdict": "买入", "score": 0.9})

        sm2 = StateMachine(db)
        sm2._ensure_table()
        recovered = sm2.recover(key)
        assert recovered is not None
        assert recovered.agent_name == "persist_agent"
        assert recovered.task == "persist_task"
        assert recovered.state == AgentState.COMPLETED
        assert recovered.result == {"verdict": "买入", "score": 0.9}
        assert recovered.trace_id == "trace_002"
        assert recovered.created_at != ""
        assert recovered.updated_at != ""

    def test_agent_layer_creates_checkpoint(self):
        db = Database(":memory:")
        db.init_db()
        mock_dl = MagicMock()
        mock_sl = MagicMock()
        agent = AgentLayer(database=db, data_layer=mock_dl, strategy_layer=mock_sl)

        mock_response = AgentResponse(
            request_id="mock_req",
            task="stock_analysis",
            code="000001",
            verdict="推荐",
            confidence=0.75,
            reasoning="mock",
            risk_flags=[],
            quality=QualityMetrics(model_used="rule", tier_used="full", latency_ms=0, providers_tried=[]),
        )

        with patch.object(agent, "_execute_pipeline", return_value=mock_response):
            request = AgentRequest(
                task="stock_analysis",
                input={"code": "000001", "date": DATE, "factor_scores": {"momentum": 0.6}},
            )
            response = agent.analyze(request)
            assert response.code == "000001"
            assert response.verdict == "推荐"

        conn = db.get_connection()
        rows = conn.execute(
            "SELECT checkpoint_key, state, result_json FROM agent_checkpoints"
        ).fetchall()
        checkpoint_found = False
        for row in rows:
            if "000001" in row["checkpoint_key"] or row["state"] == "completed":
                checkpoint_found = True
                break
        assert checkpoint_found, "No checkpoint found in database after analyze()"

    def test_state_machine_invalid_transition(self):
        db = Database(":memory:")
        db.init_db()
        sm = StateMachine(db)
        sm._ensure_table()

        key = sm.create("test_agent", "test_task", "trace_003")
        sm.transition(key, AgentState.RUNNING, "start")
        sm.transition(key, AgentState.COMPLETED, "done")

        result = sm.transition(key, AgentState.RUNNING, "should_fail")
        assert result is False

        cp = sm.get(key)
        assert cp.state == AgentState.COMPLETED

        result = sm.transition("nonexistent_key", AgentState.RUNNING, "noop")
        assert result is False