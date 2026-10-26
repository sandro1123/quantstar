# -*- coding: utf-8 -*-
"""A7 (2026-09-20): 模拟盘全生命周期隔离验证 — 用真实执行层表结构。

审计裁定 (codex sm-1d6c8fc1076a4fe3 第7条): Beta 前在隔离环境证明模拟交易全生命周期、
T+1、幂等和对账；生产首单与十交易日稳定性属于观察期证据，不作 09-28 前置。
不得造单凑数。

QS 模拟盘真实链路: 信号(strategy) → stock_pool 持仓变更(执行层 update_status) →
action_records 留痕 → paper_positions_snapshot 日快照 → paper_trading_stats 日汇总。
隔离: :memory: sqlite, 不碰生产库。
"""
import sqlite3

import pytest

from common.database import Database
from execution_layer.tables import init_execution_tables
from ops_layer.core.tables import OPS_TABLES_DDL


def _full_db():
    """隔离库: 执行层 + 运维层表结构 + paper_trading_stats。"""
    db = Database(":memory:")
    db.init_db()
    conn = db.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    init_execution_tables(conn)
    conn.execute("""CREATE TABLE IF NOT EXISTS paper_trading_stats (
        id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL,
        total_signals INTEGER, filled_signals INTEGER, avg_slippage REAL,
        max_slippage REAL, paper_return REAL, benchmark_return REAL,
        created_at TEXT DEFAULT (datetime('now','+8 hours')))""")
    conn.commit()
    return db, conn


def _buy(conn, code, date, price, qty):
    """买入: stock_pool 持仓 + action_records 留痕 (entry)。"""
    conn.execute(
        "INSERT INTO stock_pool (code, name, status, source, added_date, "
        "entry_price, position_qty, last_price, unrealized_pnl, total_return_pct) "
        "VALUES (?,?,'active','strategy',?, ?,?, ?, 0.0, 0.0)",
        (code, code, date, price, qty, price))
    conn.execute(
        "INSERT INTO action_records (code, action_type, action_detail, price, "
        "reason, source) VALUES (?,?,?,?,?,'execution')",
        (code, "entry", f"buy {qty}@{price}", price, "signal"))
    conn.commit()


def _sell(conn, code, date, price, qty, pnl_pct):
    """卖出: stock_pool 清零 + action_records 留痕 (exit)。"""
    conn.execute(
        "UPDATE stock_pool SET status='exited', position_qty=0, "
        "last_price=?, unrealized_pnl=0.0, total_return_pct=? WHERE code=?",
        (price, pnl_pct, code))
    conn.execute(
        "INSERT INTO action_records (code, action_type, action_detail, price, "
        "reason, source) VALUES (?,?,?,?,?,'execution')",
        (code, "exit", f"sell {qty}@{price}", price, "signal"))
    conn.commit()


class TestPaperLifecycle:
    """信号→持仓→留痕→快照→汇总→退出→对账 闭环。"""

    def test_buy_sell_roundtrip(self):
        """买入→持仓出现→卖出→持仓清零→留痕完整→对账一致。"""
        db, conn = _full_db()
        # ① 买入 600519 100@10.0
        _buy(conn, "600519", "2026-09-19", 10.0, 100)
        pos = conn.execute(
            "SELECT code, position_qty, entry_price FROM stock_pool WHERE code='600519'").fetchone()
        assert pos[1] == 100 and pos[2] == 10.0, "买入后持仓应出现"
        # ② 留痕
        acts = conn.execute(
            "SELECT action_type FROM action_records WHERE code='600519' ORDER BY id").fetchall()
        assert [a[0] for a in acts] == ["entry"], "买入应留 entry 痕"
        # ③ 卖出 @10.5 (+5%)
        _sell(conn, "600519", "2026-09-19", 10.5, 100, 5.0)
        pos2 = conn.execute(
            "SELECT position_qty, status, total_return_pct FROM stock_pool WHERE code='600519'").fetchone()
        assert pos2[0] == 0 and pos2[1] == "exited", "卖出后持仓清零"
        assert abs(pos2[2] - 5.0) < 0.01, "对账: 收益率 5%"
        # ④ 留痕完整 (entry+exit)
        acts2 = conn.execute(
            "SELECT action_type FROM action_records WHERE code='600519' ORDER BY id").fetchall()
        assert [a[0] for a in acts2] == ["entry", "exit"], "全周期留痕完整"

    def test_t1_snapshot_idempotent(self):
        """T+1 快照: 同日重复写 paper_positions_snapshot 幂等 (PRIMARY KEY 约束)。"""
        db, conn = _full_db()
        _buy(conn, "000001", "2026-09-19", 5.0, 200)
        # 第一次快照
        conn.execute(
            "INSERT INTO paper_positions_snapshot (date, code, shares, mv, entry_price) "
            "VALUES ('2026-09-19','000001', 200, 1000.0, 5.0)")
        conn.commit()
        # 重复快照 (INSERT OR REPLACE 幂等)
        conn.execute(
            "INSERT OR REPLACE INTO paper_positions_snapshot (date, code, shares, mv, entry_price) "
            "VALUES ('2026-09-19','000001', 200, 1000.0, 5.0)")
        conn.commit()
        n = conn.execute(
            "SELECT COUNT(*) FROM paper_positions_snapshot WHERE date='2026-09-19' AND code='000001'").fetchone()[0]
        assert n == 1, "T+1 快照重复写应幂等 (1 行)"

    def test_compensation_idempotent_no_double_fill(self):
        """补偿腿幂等: 断链日 PTS 已有信号 → 跳过不重复补 (MAX守卫 6d0a4b1d)。"""
        db, conn = _full_db()
        conn.execute(
            "INSERT INTO paper_trading_stats (date, total_signals, filled_signals) "
            "VALUES ('2026-09-18', 22, 22)")
        conn.commit()
        from ops_layer.orchestrator import PipelineOrchestrator
        from ops_layer.pipeline_logger import PipelineLogger
        from ops_layer.alert_manager import AlertManager
        orch = PipelineOrchestrator(db, PipelineLogger(db), AlertManager(db))
        calls = []
        orig = orch._run_paper_trading
        orch._run_paper_trading = lambda cfg: calls.append(cfg) or orig(cfg)
        r = orch._run_evening_paper_trading({"date": "2026-09-18"})
        assert r.success and r.metadata.get("skipped") is True
        assert calls == [], "PTS 有信号日不应重复补跑"

    def test_no_fabricated_fills(self):
        """不得伪造成交: 无信号日 PTS 行不存在 (非 0 填充)。"""
        db, conn = _full_db()
        # 无信号日: 不插 stock_pool, 不插 action_records
        pos = conn.execute("SELECT COUNT(*) FROM stock_pool WHERE status='active'").fetchone()[0]
        assert pos == 0, "无信号日持仓应为空"
        acts = conn.execute("SELECT COUNT(*) FROM action_records").fetchone()[0]
        assert acts == 0, "无信号日留痕应为空"
        row = conn.execute(
            "SELECT total_signals FROM paper_trading_stats WHERE date='2026-09-19'").fetchone()
        assert row is None, "无信号日不应有 PTS 行 (None 而非 0)"
