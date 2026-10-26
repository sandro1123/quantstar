"""DL-16: 二级索引 — ensure_indexes/list_date_indexes (in-memory, 无生产写)"""

import sqlite3

from data_layer.indexes import (
    DATE_INDEX_TABLES, INDEX_DDL, ensure_indexes, list_date_indexes,
)


class _FakeDB:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

    def get_connection(self):
        return self.conn


def _mk_db():
    db = _FakeDB()
    for t in DATE_INDEX_TABLES:
        db.conn.execute(
            f"CREATE TABLE {t} (code TEXT, date TEXT, PRIMARY KEY (code, date))")
    db.conn.commit()
    return db


def test_index_ddl_covers_four_tables():
    assert len(DATE_INDEX_TABLES) == 4
    for t in DATE_INDEX_TABLES:
        assert f"idx_{t}_date" in INDEX_DDL
        assert f"ON {t}(date)" in INDEX_DDL

def test_ensure_indexes_creates_all():
    db = _mk_db()
    result = ensure_indexes(db)
    assert all(result.values())
    names = list_date_indexes(db)
    for t in DATE_INDEX_TABLES:
        assert f"idx_{t}_date" in names

def test_ensure_indexes_idempotent():
    db = _mk_db()
    ensure_indexes(db)
    n1 = len(list_date_indexes(db))
    result = ensure_indexes(db)  # 重跑不报错 (IF NOT EXISTS)
    n2 = len(list_date_indexes(db))
    assert all(result.values())
    assert n1 == n2 == 4

def test_ensure_indexes_missing_table_fails_gracefully():
    """缺表 → 该表 False, 其余照常 (不抛错)"""
    db = _FakeDB()
    db.conn.execute(
        "CREATE TABLE daily_klines (code TEXT, date TEXT, PRIMARY KEY (code, date))")
    db.conn.commit()
    result = ensure_indexes(db)
    assert result["daily_klines"] is True
    assert result["stock_valuation"] is False

def test_list_date_indexes_empty():
    db = _FakeDB()
    assert list_date_indexes(db) == []

def test_query_plan_uses_index_after_ensure():
    """建索引后 date 查询走 SEARCH 而非 SCAN"""
    db = _mk_db()
    ensure_indexes(db)
    plan = db.conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM daily_klines WHERE date = ?",
        ("2026-08-15",)).fetchall()
    assert "idx_daily_klines_date" in plan[0][3]
