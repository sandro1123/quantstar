"""Tests for common/database.py"""
import pytest
from common.database import Database


def test_database_in_memory():
    db = Database(":memory:")
    conn = db.get_connection()
    assert conn is not None
    db.close()


def test_database_create_table():
    db = Database(":memory:")
    conn = db.get_connection()
    conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO test VALUES (1, 'hello')")
    row = conn.execute("SELECT name FROM test WHERE id=1").fetchone()
    assert row[0] == "hello"
    db.close()


def test_database_init_db():
    db = Database(":memory:")
    db.init_db()
    conn = db.get_connection()
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    table_names = [t[0] for t in tables]
    assert "schema_version" in table_names
    db.close()


def test_database_schema_version():
    db = Database(":memory:")
    db.init_db()
    conn = db.get_connection()
    row = conn.execute("SELECT version FROM schema_version ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None
    assert row[0] >= 1
    db.close()


def test_get_connection_recovers_after_caller_close(tmp_path):
    """P0 回归: 调用方对共享缓存连接直接 .close() 后, get_connection 必须
    返回可用连接 (重连), 而不是返回已关闭对象抛
    "Cannot operate on a closed database"。

    生产事故 (2026-08-26 factor_update 3/3 确定性失败): orchestrator
    _run_factor_update 对 get_connection() 返回的共享连接 conn.close(),
    缓存中毒化, 后续落库校验 SELECT 抛 ProgrammingError。
    """
    db = Database(str(tmp_path / "t.db"))
    db.init_db()
    c1 = db.get_connection()
    c1.close()  # 调用方误关共享连接 (毒化缓存)
    c2 = db.get_connection()
    # 必须返回可用连接 (可以是新对象), 且能正常执行查询
    row = c2.execute("SELECT version FROM schema_version").fetchone()
    assert row[0] >= 1
    db.close()


def test_get_connection_stable_when_not_closed(tmp_path):
    """正常路径不回归: 连接未被关闭时 get_connection 返回同一缓存对象。"""
    db = Database(str(tmp_path / "t.db"))
    c1 = db.get_connection()
    c2 = db.get_connection()
    assert c2 is c1
    db.close()


def test_is_closed_probe():
    """_is_closed 探针: 打开连接 False, 关闭连接 True。"""
    import sqlite3
    conn = sqlite3.connect(":memory:")
    assert Database._is_closed(conn) is False
    conn.close()
    assert Database._is_closed(conn) is True