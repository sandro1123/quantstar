# -*- coding: utf-8 -*-
"""exec_stats 时点/源错配修复 (2026-09-20) — W2_CARD_EXECSTATS_0920。

病根（生产实证，双病）:
  病①「读错源」: save_daily 的 executed_entries 读 action_records（带 skip_entry 噪音、
     且该表在本 run 未落定），而 total_trades 读 stock_pool（本 run 已落定）→ 前者恒 0。
     实证: 09-14~09-18 exec_stats.executed_entries=0，而 action_records 同日有 5~21 笔 entry。
  病②「统计早于写入」: entry_date/action_records.entry 由 paper_broker 在 **16:35**
     写入（同事务相邻两行），而 save_daily 挂 daily_update（cron **16:16**）→ 早 19 分钟。

修法（丙案 + 乙案合并 = 换对源 + 在对的时点算）:
  - 源: executed_entries 改读 `stock_pool WHERE entry_date = ?`（与 total_trades 同表同落定）
  - 时点: save_daily 由独立收盘腿在 paper_trading 之后调用（幂等重算）

锚点适用窗口下界（审计官 sm-440adb5a 要求写进注释）:
  `stock_pool.entry_date` 与 `action_records.entry` 从 **2026-08-12** 起逐日对拍一致
  （17/17 天实证）；**2026-07-24 及更早不一致**（A=0 vs C=3，更早的入池路径未同步写
  entry_date）。故 executed_entries 的 entry_date 口径**只对 >= 2026-08-12 的日期成立**；
  更早日期不得据此回填。⚠️ 未来若有人拿本口径去数 07 月会静默错。

陷阱（施工中踩过）:
  `stock_pool.updated_at` **不可**用于验证写入时刻——被后续 UPDATE 反复覆盖
  （实测 09-17 的 entry 行 updated_at = 09-18 16:16）。唯一可靠写入时刻证据 =
  `action_records.created_at`（只写一次，不可变）。
"""
import sqlite3

import pytest

from common.database import Database
from ops_layer.core.tables import OPS_TABLES_DDL
from execution_layer.tables import init_execution_tables


def _db():
    db = Database(":memory:")
    db.init_db()
    conn = db.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    init_execution_tables(conn)
    conn.commit()
    return db, conn


def _add_pool(conn, code, status, entry_date, exit_date=None, entry_price=10.0):
    conn.execute(
        "INSERT INTO stock_pool (code, name, status, source, added_date, entry_date, "
        "exit_date, entry_price, position_qty, last_price) "
        "VALUES (?,?,'" + status + "','strategy','2026-09-18',?,?,?,0,10.0)",
        (code, code, entry_date, exit_date, entry_price))
    conn.commit()


def _add_action(conn, code, action_type, date):
    conn.execute(
        "INSERT INTO action_records (code, action_type, reason, source, created_at) "
        "VALUES (?,?,?,'execution', ?)",
        (code, action_type, "test", f"{date} 16:35:00"))
    conn.commit()


def _stats(conn, date):
    return conn.execute(
        "SELECT executed_entries, total_recommendations, entry_rate FROM execution_stats "
        "WHERE period='daily' AND period_end=?", (date,)).fetchone()


# ---- 病①: 源错配 —— executed_entries 须免受 action_records 噪音/时点影响 ----

def test_executed_entries_reads_stock_pool_not_action_records():
    """核心: 已退出的当日 entry 也须计入 (stock_pool 无 status 限定)。

    反例钉死: 若按 status='active' 过滤, 历史日已退出的 entry 会被低估为 0。
    """
    from execution_layer.stats.performance import ExecutionStats
    db, conn = _db()
    # 当日 3 笔 entry: 1 笔仍 active, 2 笔已 exited (历史日回看时会被 status 过滤漏掉)
    _add_pool(conn, "600001", "active", "2026-09-18")
    _add_pool(conn, "600002", "exited", "2026-09-18", exit_date="2026-09-19")
    _add_pool(conn, "600003", "exited", "2026-09-18", exit_date="2026-09-19")
    # action_records 里只留 1 笔 (模拟 save_daily 跑时其余尚未落库)
    _add_action(conn, "600001", "entry", "2026-09-18")

    ExecutionStats(db).save_daily("2026-09-18")
    ex, rec, rate = _stats(conn, "2026-09-18")
    assert ex == 3, f"executed_entries 须=3 (读 stock_pool 无 status 过滤), 实得 {ex}"


def test_executed_entries_immune_to_skip_entry_noise():
    """skip_entry 噪音不得计入成交数。"""
    from execution_layer.stats.performance import ExecutionStats
    db, conn = _db()
    _add_pool(conn, "600001", "active", "2026-09-18")
    for i in range(100):
        _add_action(conn, f"60{i:04d}", "skip_entry", "2026-09-18")
    _add_action(conn, "600001", "entry", "2026-09-18")

    ExecutionStats(db).save_daily("2026-09-18")
    ex, rec, rate = _stats(conn, "2026-09-18")
    assert ex == 1, f"100 笔 skip_entry 不得计入, 应=1 实得 {ex}"


def test_entry_rate_denominator_unchanged():
    """entry_rate 分母仍= set_entry_range (推荐数), 语义不变。"""
    from execution_layer.stats.performance import ExecutionStats
    db, conn = _db()
    _add_pool(conn, "600001", "active", "2026-09-18")
    _add_pool(conn, "600002", "active", "2026-09-18")
    for i in range(4):
        _add_action(conn, f"60{i:04d}", "set_entry_range", "2026-09-18")

    ExecutionStats(db).save_daily("2026-09-18")
    ex, rec, rate = _stats(conn, "2026-09-18")
    assert rec == 4, f"推荐数应=4 实得 {rec}"
    assert ex == 2, f"成交数应=2 实得 {ex}"
    assert abs(rate - 0.5) < 0.01, f"entry_rate 应=0.5 实得 {rate}"


# ---- 反向锚: 防未来字段写串 ----

def test_added_date_differs_from_entry_date():
    """反向锚: added_date(候选) ≠ entry_date(成交) 须可不同, 防字段写串。

    生产实证 09-18: added_date=22 (候选) vs entry_date=21 (成交)。
    executed_entries 锚的是 entry_date, 不得误锚 added_date。
    """
    from execution_layer.stats.performance import ExecutionStats
    db, conn = _db()
    # 2 个候选入池 (added_date=当日), 只有 1 个真成交 (entry_date=当日)
    conn.execute("INSERT INTO stock_pool (code, name, status, source, added_date, "
                 "entry_date, position_qty) VALUES ('600001','A','active','strategy',"
                 "'2026-09-18','2026-09-18',100)")
    conn.execute("INSERT INTO stock_pool (code, name, status, source, added_date, "
                 "position_qty) VALUES ('600002','B','watch','strategy','2026-09-18',0)")
    conn.commit()
    for i in range(2):
        _add_action(conn, f"60{i:04d}", "set_entry_range", "2026-09-18")

    ExecutionStats(db).save_daily("2026-09-18")
    ex, rec, rate = _stats(conn, "2026-09-18")
    assert rec == 2, "推荐数 (set_entry_range) 应=2"
    assert ex == 1, f"成交数须锚 entry_date (1 笔), 不得锚 added_date (2 笔), 实得 {ex}"


# ---- 幂等 ----

def test_save_daily_idempotent():
    """同日重跑不堆行, 数字不变 (独立腿重算依赖此性质)。"""
    from execution_layer.stats.performance import ExecutionStats
    db, conn = _db()
    _add_pool(conn, "600001", "active", "2026-09-18")
    ExecutionStats(db).save_daily("2026-09-18")
    ExecutionStats(db).save_daily("2026-09-18")
    n = conn.execute("SELECT COUNT(*) FROM execution_stats WHERE period='daily' "
                     "AND period_end='2026-09-18'").fetchone()[0]
    assert n == 1, f"幂等失败, 堆了 {n} 行"
    assert _stats(conn, "2026-09-18")[0] == 1


# ---- 时点: 独立腿存在性 ----

def test_stats_finalize_leg_after_paper_trading():
    """病②: 须存在晚于 paper_trading 的 stats 收尾入口 (不能在 16:16 算)。"""
    import inspect
    from ops_layer import orchestrator
    src = inspect.getsource(orchestrator)
    assert "stats_finalize" in src or "execution_stats_finalize" in src, \
        "缺收盘后 stats 收尾腿 — 16:16 算必看不到 16:35 的 entry"
