"""MemoryStore 读写 API 测试 — 三铁律: 数据不是指令 / 读取有预算 / 写入有闸门"""
import pytest
from common.database import Database
from execution_layer.migrations import ensure_schema
from memory_layer.store import MemoryStore


@pytest.fixture
def store():
    db = Database(":memory:")
    ensure_schema(db)
    return MemoryStore(db)


class TestWriteGate:
    def test_write_without_evidence_rejected(self, store):
        assert store.write("global", "lesson", "内容", evidence="") is None
        assert store.write("global", "lesson", "内容", evidence="   ") is None

    def test_write_invalid_scope_rejected(self, store):
        assert store.write("bogus:xx", "lesson", "内容", evidence="src:1") is None

    def test_write_invalid_kind_rejected(self, store):
        assert store.write("global", "bogus", "内容", evidence="src:1") is None

    def test_write_valid_returns_id(self, store):
        mid = store.write("stock:000001", "feedback", "内容", evidence="agent_evaluate:2026-08-28:hits=3")
        assert mid is not None and mid > 0

    def test_conflict_supersedes_old(self, store):
        old = store.write("agent:evaluator", "feedback", "旧", evidence="src:1")
        new = store.write("agent:evaluator", "feedback", "新", evidence="src:2")
        rows = store._conn().execute(
            "SELECT id, status, superseded_by FROM memory_store ORDER BY id").fetchall()
        assert rows[0][1] == "superseded" and rows[0][2] == new
        assert rows[1][1] == "active"

    def test_content_truncated_to_500(self, store):
        mid = store.write("global", "lesson", "长" * 800, evidence="src:1")
        row = store._conn().execute(
            "SELECT content FROM memory_store WHERE id=?", (mid,)).fetchone()
        assert len(row[0]) == 500


class TestRead:
    def test_scope_filter_includes_global(self, store):
        store.write("global", "lesson", "全局", evidence="s:1")
        store.write("stock:000001", "lesson", "个股", evidence="s:2")
        store.write("stock:000002", "lesson", "他股", evidence="s:3")
        mems = store.read(scope="stock:000001")
        contents = {m.content for m in mems}
        assert contents == {"全局", "个股"}

    def test_expired_not_returned(self, store):
        store.write("global", "lesson", "过期", evidence="s:1", expires_at="2020-01-01")
        assert store.read(scope="global") == []

    def test_superseded_not_returned(self, store):
        store.write("global", "lesson", "旧", evidence="s:1")
        store.write("global", "lesson", "新", evidence="s:2")
        mems = store.read(scope="global")
        assert [m.content for m in mems] == ["新"]

    def test_top_k_limit(self, store):
        for i in range(8):
            store.write(f"stock:{i:06d}", "lesson", f"内容{i}", evidence=f"s:{i}")
        assert len(store.read(scope=None, top_k=3)) == 3

    def test_token_budget_caps_result(self, store):
        for i in range(10):
            store.write(f"stock:{i:06d}", "lesson", "字" * 300, evidence=f"s:{i}")
        mems = store.read(scope=None, top_k=10, token_budget=500)
        total_chars = sum(len(m.content) for m in mems)
        assert total_chars <= 500 * 2  # 中文 1 字 ≈ 0.67 token 上限的宽松上界
        assert len(mems) < 10
