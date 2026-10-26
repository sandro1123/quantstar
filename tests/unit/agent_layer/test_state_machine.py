"""StateMachine 单元测试"""
import pytest
from agent_layer.core.state_machine import AgentState, StateTransition, AgentCheckpoint, StateMachine
from common.database import Database


def test_agent_state_enum_values():
    assert AgentState.PENDING.value == "pending"
    assert AgentState.RUNNING.value == "running"
    assert AgentState.COMPLETED.value == "completed"
    assert AgentState.FAILED.value == "failed"
    assert AgentState.DEGRADED.value == "degraded"
    assert AgentState.TIMEOUT.value == "timeout"


def test_state_machine_create_without_db():
    sm = StateMachine()
    assert sm.db is None
    assert sm._checkpoints == {}


def test_state_machine_create_with_db():
    db = Database(":memory:")
    sm = StateMachine(db=db)
    assert sm.db is db


def test_create_returns_valid_key():
    sm = StateMachine()
    key = sm.create("test_agent", "test_task", "trace_001")
    assert key == "test_agent:test_task:trace_001"


def test_create_sets_state_to_pending():
    sm = StateMachine()
    key = sm.create("agent_a", "task_a", "trace_002")
    cp = sm.get(key)
    assert cp is not None
    assert cp.state == AgentState.PENDING


def test_valid_transition_pending_to_running():
    sm = StateMachine()
    key = sm.create("agent", "task", "t1")
    assert sm.transition(key, AgentState.RUNNING) is True
    assert sm.get(key).state == AgentState.RUNNING


def test_valid_transition_running_to_completed():
    sm = StateMachine()
    key = sm.create("agent", "task", "t2")
    sm.transition(key, AgentState.RUNNING)
    assert sm.transition(key, AgentState.COMPLETED) is True
    assert sm.get(key).state == AgentState.COMPLETED


def test_valid_transition_running_to_failed():
    sm = StateMachine()
    key = sm.create("agent", "task", "t3")
    sm.transition(key, AgentState.RUNNING)
    assert sm.transition(key, AgentState.FAILED) is True
    assert sm.get(key).state == AgentState.FAILED


def test_valid_transition_running_to_degraded():
    sm = StateMachine()
    key = sm.create("agent", "task", "t4")
    sm.transition(key, AgentState.RUNNING)
    assert sm.transition(key, AgentState.DEGRADED) is True
    assert sm.get(key).state == AgentState.DEGRADED


def test_valid_transition_degraded_to_completed():
    sm = StateMachine()
    key = sm.create("agent", "task", "t5")
    sm.transition(key, AgentState.RUNNING)
    sm.transition(key, AgentState.DEGRADED)
    assert sm.transition(key, AgentState.COMPLETED) is True
    assert sm.get(key).state == AgentState.COMPLETED


def test_valid_transition_failed_to_pending():
    sm = StateMachine()
    key = sm.create("agent", "task", "t6")
    sm.transition(key, AgentState.RUNNING)
    sm.transition(key, AgentState.FAILED)
    assert sm.transition(key, AgentState.PENDING) is True
    assert sm.get(key).state == AgentState.PENDING


def test_valid_transition_timeout_to_pending():
    sm = StateMachine()
    key = sm.create("agent", "task", "t7")
    sm.transition(key, AgentState.RUNNING)
    sm.transition(key, AgentState.TIMEOUT)
    assert sm.transition(key, AgentState.PENDING) is True
    assert sm.get(key).state == AgentState.PENDING


def test_valid_transition_pending_to_failed():
    sm = StateMachine()
    key = sm.create("agent", "task", "t8")
    assert sm.transition(key, AgentState.FAILED) is True
    assert sm.get(key).state == AgentState.FAILED


def test_invalid_transition_pending_to_completed():
    sm = StateMachine()
    key = sm.create("agent", "task", "t9")
    assert sm.transition(key, AgentState.COMPLETED) is False
    assert sm.get(key).state == AgentState.PENDING


def test_invalid_transition_running_to_pending():
    sm = StateMachine()
    key = sm.create("agent", "task", "t10")
    sm.transition(key, AgentState.RUNNING)
    assert sm.transition(key, AgentState.PENDING) is False
    assert sm.get(key).state == AgentState.RUNNING


def test_invalid_transition_completed_to_anything():
    sm = StateMachine()
    key = sm.create("agent", "task", "t11")
    sm.transition(key, AgentState.RUNNING)
    sm.transition(key, AgentState.COMPLETED)
    assert sm.transition(key, AgentState.PENDING) is False
    assert sm.transition(key, AgentState.RUNNING) is False
    assert sm.transition(key, AgentState.FAILED) is False
    assert sm.transition(key, AgentState.DEGRADED) is False
    assert sm.transition(key, AgentState.TIMEOUT) is False
    assert sm.get(key).state == AgentState.COMPLETED


def test_transition_returns_false_for_invalid_transition():
    sm = StateMachine()
    key = sm.create("agent", "task", "t12")
    assert sm.transition(key, AgentState.COMPLETED) is False


def test_transition_returns_false_for_non_existent_key():
    sm = StateMachine()
    assert sm.transition("nonexistent", AgentState.RUNNING) is False


def test_transition_stores_reason_in_error_when_failed():
    sm = StateMachine()
    key = sm.create("agent", "task", "t13")
    sm.transition(key, AgentState.RUNNING)
    sm.transition(key, AgentState.FAILED, reason="timeout after 30s")
    assert sm.get(key).error == "timeout after 30s"


def test_transition_appends_to_transitions_list():
    sm = StateMachine()
    key = sm.create("agent", "task", "t14")
    sm.transition(key, AgentState.RUNNING, reason="started")
    sm.transition(key, AgentState.COMPLETED, reason="done")
    cp = sm.get(key)
    assert len(cp.transitions) == 2
    assert cp.transitions[0].from_state == AgentState.PENDING
    assert cp.transitions[0].to_state == AgentState.RUNNING
    assert cp.transitions[0].reason == "started"
    assert cp.transitions[1].from_state == AgentState.RUNNING
    assert cp.transitions[1].to_state == AgentState.COMPLETED
    assert cp.transitions[1].reason == "done"


def test_set_result_updates_result_dict():
    sm = StateMachine()
    key = sm.create("agent", "task", "t15")
    sm.set_result(key, {"score": 0.95, "signal": "buy"})
    assert sm.get(key).result == {"score": 0.95, "signal": "buy"}


def test_get_returns_none_for_non_existent_key():
    sm = StateMachine()
    assert sm.get("nope") is None


def test_get_returns_checkpoint_after_operations():
    sm = StateMachine()
    key = sm.create("agent_a", "task_b", "trace_c")
    sm.transition(key, AgentState.RUNNING)
    sm.transition(key, AgentState.COMPLETED, reason="success")
    sm.set_result(key, {"output": "ok"})
    cp = sm.get(key)
    assert cp.agent_name == "agent_a"
    assert cp.task == "task_b"
    assert cp.trace_id == "trace_c"
    assert cp.state == AgentState.COMPLETED
    assert cp.result == {"output": "ok"}
    assert cp.error == ""
    assert len(cp.transitions) == 2


def test_recover_without_db_returns_none():
    sm = StateMachine()
    result = sm.recover("any_key")
    assert result is None


def test_recover_with_db_reads_from_sqlite():
    db = Database(":memory:")
    db.get_connection()
    sm = StateMachine(db=db)
    sm._ensure_table()
    key = sm.create("agent_x", "task_y", "trace_z")
    sm.transition(key, AgentState.RUNNING, reason="go")
    sm.transition(key, AgentState.COMPLETED, reason="done")
    sm.set_result(key, {"answer": 42})

    sm2 = StateMachine(db=db)
    cp = sm2.recover(key)
    assert cp is not None
    assert cp.agent_name == "agent_x"
    assert cp.task == "task_y"
    assert cp.trace_id == "trace_z"
    assert cp.state == AgentState.COMPLETED
    assert cp.result == {"answer": 42}
    assert cp.error == ""
    assert cp.created_at != ""
    assert cp.updated_at != ""
    assert len(cp.transitions) == 0


def test_ensure_table_creates_table():
    db = Database(":memory:")
    conn = db.get_connection()
    sm = StateMachine(db=db)
    sm._ensure_table()
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='agent_checkpoints'")
    assert cursor.fetchone() is not None


def test_ensure_table_no_db():
    sm = StateMachine()
    sm._ensure_table()


def test_full_lifecycle():
    sm = StateMachine()
    key = sm.create("lifecycle_agent", "lifecycle_task", "lifecycle_001")
    assert sm.get(key).state == AgentState.PENDING

    assert sm.transition(key, AgentState.RUNNING, reason="starting execution") is True
    assert sm.get(key).state == AgentState.RUNNING

    assert sm.transition(key, AgentState.COMPLETED, reason="finished successfully") is True
    assert sm.get(key).state == AgentState.COMPLETED

    sm.set_result(key, {"status": "ok", "value": 100})
    cp = sm.get(key)
    assert cp.agent_name == "lifecycle_agent"
    assert cp.task == "lifecycle_task"
    assert cp.trace_id == "lifecycle_001"
    assert cp.result == {"status": "ok", "value": 100}
    assert cp.error == ""
    assert len(cp.transitions) == 2
    assert cp.transitions[0].from_state == AgentState.PENDING
    assert cp.transitions[0].to_state == AgentState.RUNNING
    assert cp.transitions[0].reason == "starting execution"
    assert cp.transitions[1].from_state == AgentState.RUNNING
    assert cp.transitions[1].to_state == AgentState.COMPLETED
    assert cp.transitions[1].reason == "finished successfully"
    assert cp.created_at != ""
    assert cp.updated_at != ""
    assert cp.created_at <= cp.updated_at