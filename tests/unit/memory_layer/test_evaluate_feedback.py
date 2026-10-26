"""agent_evaluate 结果回流 → feedback 记忆 (Phase1 唯一自动写入源)"""
import pytest
from common.database import Database
from execution_layer.migrations import ensure_schema
from memory_layer.feedback import record_evaluate_feedback
from memory_layer.store import MemoryStore


@pytest.fixture
def db():
    d = Database(":memory:")
    ensure_schema(d)
    return d


def test_feedback_written_when_evaluated(db):
    mid = record_evaluate_feedback(
        db, date="2026-08-28",
        meta={"evaluated": 10, "hits": 6, "bt_hit_rate": 0.6, "bt_unevaluated": 2})
    assert mid is not None
    mems = MemoryStore(db).read(scope="agent:evaluator", kind="feedback")
    assert len(mems) == 1
    assert "60%" in mems[0].content
    assert mems[0].evidence.startswith("agent_evaluate:2026-08-28:")


def test_no_feedback_when_zero_evaluated(db):
    mid = record_evaluate_feedback(db, date="2026-08-28", meta={"evaluated": 0, "hits": 0})
    assert mid is None
    assert MemoryStore(db).read(scope="agent:evaluator") == []


def test_feedback_failure_does_not_raise(db):
    # 坏 db 不得抛出 (回流失败不阻断管道, 诚实降级)
    mid = record_evaluate_feedback(None, date="2026-08-28", meta={"evaluated": 5, "hits": 3})
    assert mid is None
