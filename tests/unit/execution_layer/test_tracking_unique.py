"""W1 任务D: post_exit_tracking 防重复 — 同 (code, exit_date, check_point) 唯一。

病: create_tracking 用 INSERT OR IGNORE + 自增主键, 重复创建产生双份
T+5/T+10/T+20 行; 旧测试 test_create_tracking_allows_duplicate_checkpoints
把 6 行断言成正确行为 = 把病锁进契约 (判例: 断言病态行为的测试, 改它才是修完)。
"""
import sqlite3

import pytest

from common.database import Database
from execution_layer import ExecutionLayer
from execution_layer.tables import init_execution_tables


@pytest.fixture
def exec_layer():
    db = Database(":memory:")
    db.init_db()
    el = ExecutionLayer(db=db)
    el.start()
    yield el
    el.shutdown()
    db.close()


def test_duplicate_create_tracking_deduplicates(exec_layer):
    for _ in range(2):
        exec_layer.post_exit_tracker.create_tracking(
            code="000001", exit_date="2026-07-15", exit_price=15.0,
            entry_price=10.0, total_return_pct=50.0, exit_reason="take_profit")
    records = exec_layer.post_exit_tracker.get_tracking("000001")
    assert len(records) == 3
    assert {r["check_point"] for r in records} == {"T+5", "T+10", "T+20"}


# 无 UNIQUE 的旧库形态 (迁移前 post_exit_tracking)
_LEGACY_TRACKING_DDL = """
CREATE TABLE post_exit_tracking (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    exit_date TEXT NOT NULL,
    exit_price REAL NOT NULL,
    entry_price REAL NOT NULL,
    total_return_pct REAL NOT NULL,
    exit_reason TEXT NOT NULL,
    check_point TEXT NOT NULL CHECK (check_point IN ('T+5', 'T+10', 'T+20')),
    check_date TEXT,
    current_price REAL,
    price_change_since_exit REAL,
    quality TEXT CHECK (quality IN ('correct', 'neutral', 'wrong', NULL)),
    quality_detail TEXT,
    review_note TEXT,
    created_at TEXT DEFAULT (datetime('now', '+8 hours'))
)
"""


def test_legacy_table_with_duplicates_refuses_upgrade_loudly():
    """旧库已有重复行时执行层建表升级必须显性失败 (宁缺勿脏), 不静默删历史。"""
    db = Database(":memory:")
    try:
        conn = db.get_connection()
        conn.execute(_LEGACY_TRACKING_DDL)
        for _ in range(2):
            conn.execute(
                "INSERT INTO post_exit_tracking (code, exit_date, exit_price, entry_price, "
                "total_return_pct, exit_reason, check_point) "
                "VALUES ('000001', '2026-07-15', 15.0, 10.0, 50.0, 'take_profit', 'T+5')")
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            init_execution_tables(conn)
    finally:
        db.close()


def test_legacy_clean_table_upgrade_adds_unique_index():
    db = Database(":memory:")
    try:
        conn = db.get_connection()
        conn.execute(_LEGACY_TRACKING_DDL)
        conn.execute(
            "INSERT INTO post_exit_tracking (code, exit_date, exit_price, entry_price, "
            "total_return_pct, exit_reason, check_point) "
            "VALUES ('000001', '2026-07-15', 15.0, 10.0, 50.0, 'take_profit', 'T+5')")
        conn.commit()
        init_execution_tables(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO post_exit_tracking (code, exit_date, exit_price, entry_price, "
                "total_return_pct, exit_reason, check_point) "
                "VALUES ('000001', '2026-07-15', 15.0, 10.0, 50.0, 'take_profit', 'T+5')")
    finally:
        db.close()
