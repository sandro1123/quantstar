"""AUD-001 回归: memory_store 两条建表路径必须同构。

migrations.py v5 (中央清单) 与 memory_layer/store.py _ensure_mem_schema (全新库路径)
若不同源, 测试库与生产库 schema 语义分叉 —— 测试验证的不是生产形态 (假绿族)。
实测分叉三处: created_at 默认 UTC vs BJT / 丢 NOT NULL / 丢两个索引。
"""
from common.database import Database
from memory_layer.store import MemoryStore


def _schema_of(conn, name):
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return row[0] if row else ""


def test_fresh_db_created_at_is_bjt_like_migrations():
    """全新库建表的 created_at 默认必须是 BJT (+8 hours), 与 migrations v5 同源。"""
    db = Database(":memory:")
    MemoryStore(db)
    sql = _schema_of(db.get_connection(), "memory_store")
    assert "+8 hours" in sql, (
        "store.py 建表路径写出 UTC 默认, 与 migrations v5 的 BJT 分叉: %s" % sql)


def test_fresh_db_has_both_indexes():
    """全新库必须带 migrations v5 的两个索引, 否则读路径性能与生产不同构。"""
    db = Database(":memory:")
    MemoryStore(db)
    names = {r[0] for r in db.get_connection().execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='memory_store'")}
    assert "idx_memory_store_scope" in names
    assert "idx_memory_store_created" in names
