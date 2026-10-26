# -*- coding: utf-8 -*-
"""A12 加固 (审计官 sm-5e5f5c8bc9484cc9 要求): executed_entries 与 total_trades 同源锚。

审计官硬要求:
  「不能只看数字对, 须看『与 total_trades 同源 + 可独立复现』」

设计 (同源自比法, 判例: 判据不应依赖「正确答案是什么」而应依赖「被测对象是否响应输入」):
  构造 **action_records 完全为空** 的库, 只填 stock_pool。
  若 executed_entries / total_trades 都读 stock_pool → 两者都能出非零数;
  若任一读 action_records → 该指标必为 0。
  → 不依赖外部真值, 只问「被测对象是否响应 stock_pool 这一输入」。

这比「数字对拍」更强: 数字对拍在 action_records 恰好与 stock_pool 同步时会假绿,
同源自比则把「读哪张表」直接变成可观测行为。
"""
import sqlite3

import pytest

from common.database import Database
from ops_layer.core.tables import OPS_TABLES_DDL
from execution_layer.tables import init_execution_tables


def _db_no_actions():
    """只建 stock_pool, **不写任何 action_records**。"""
    db = Database(":memory:")
    db.init_db()
    conn = db.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    init_execution_tables(conn)
    conn.commit()
    return db, conn


def _pool(conn, code, status, entry_date=None, exit_date=None):
    conn.execute(
        "INSERT INTO stock_pool (code, name, status, source, added_date, "
        "entry_date, exit_date, entry_price, position_qty, last_price) "
        "VALUES (?,?,?,'strategy','2026-09-18',?,?,10.0,0,10.0)",
        (code, code, status, entry_date, exit_date))
    conn.commit()


def test_both_metrics_survive_empty_action_records():
    """同源自比: action_records 全空时, 两个指标都须给出非零 (证明同读 stock_pool)。"""
    from execution_layer.stats.performance import ExecutionStats
    db, conn = _db_no_actions()
    n_actions = conn.execute("SELECT COUNT(*) FROM action_records").fetchone()[0]
    assert n_actions == 0, "前置: action_records 必须为空"
    # entry 2 笔 (1 active + 1 exited), exit 3 笔
    _pool(conn, "600001", "active", entry_date="2026-09-18")
    _pool(conn, "600002", "exited", entry_date="2026-09-18", exit_date="2026-09-18")
    for i in range(3):
        _pool(conn, f"60001{i}", "exited", exit_date="2026-09-18")

    ExecutionStats(db).save_daily("2026-09-18")
    row = conn.execute(
        "SELECT executed_entries, total_trades FROM execution_stats "
        "WHERE period='daily' AND period_end='2026-09-18'").fetchone()
    assert row is not None, "统计行未写入"
    ex, tot = row
    # 两者都必须非零 —— 任一读 action_records 就必然是 0
    assert ex == 2, f"executed_entries 须=2 (只 stock_pool 有数), 实得 {ex} — 说明仍读 action_records"
    # 注: 600002 同设 entry_date+exit_date → 既是 1 笔 entry 又是 1 笔 exit,
    # 故 exit 侧共 4 笔 (600002 + 3 个 60001x)
    assert tot == 4, f"total_trades 须=4, 实得 {tot}"
    assert ex > 0 and tot > 0, "同源自比失败: 有数据但指标为 0"


def test_executed_entries_responds_to_stock_pool_input():
    """响应输入: 增删 stock_pool 行 → executed_entries 随之变 (证明它读该表)。"""
    from execution_layer.stats.performance import ExecutionStats
    db, conn = _db_no_actions()
    _pool(conn, "600001", "active", entry_date="2026-09-18")
    ExecutionStats(db).save_daily("2026-09-18")
    v1 = conn.execute("SELECT executed_entries FROM execution_stats "
                      "WHERE period_end='2026-09-18'").fetchone()[0]
    # 增一行
    _pool(conn, "600002", "active", entry_date="2026-09-18")
    ExecutionStats(db).save_daily("2026-09-18")
    v2 = conn.execute("SELECT executed_entries FROM execution_stats "
                      "WHERE period_end='2026-09-18'").fetchone()[0]
    assert v1 == 1, f"初值应=1 实得 {v1}"
    assert v2 == 2, f"增一行后应=2 实得 {v2} — 未响应 stock_pool 输入"
