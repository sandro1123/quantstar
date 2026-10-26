"""memory_store 表迁移测试 (v5, 中央清单 execution_layer/migrations.py)

表名裁定: 既有 agent_memory 是预测缓存 (quality/repository.py), 新记忆表命名
memory_store 避免撞车。
"""
from common.database import Database
from execution_layer.migrations import MIGRATIONS, TARGET_VERSION, ensure_schema


def test_migration_v5_creates_memory_store():
    db = Database(":memory:")
    result = ensure_schema(db)
    conn = db.get_connection()
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_store'"
    ).fetchone() is not None
    cols = {r[1] for r in conn.execute("PRAGMA table_info(memory_store)")}
    assert {"scope", "kind", "content", "evidence", "confidence",
            "created_at", "expires_at", "status", "superseded_by"} <= cols
    assert TARGET_VERSION >= 5
    assert result["version_after"] == TARGET_VERSION


def test_migration_v5_idempotent():
    db = Database(":memory:")
    ensure_schema(db)
    result = ensure_schema(db)
    assert result["applied"] == []
    conn = db.get_connection()
    n = conn.execute("SELECT count(*) FROM memory_store").fetchone()[0]
    assert n == 0
