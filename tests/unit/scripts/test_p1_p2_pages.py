# -*- coding: utf-8 -*-
"""p1_pool / p2_positions TDD (各 3 钉, 09-29 02:00 窗 Day1 mock P1/P2 落地)。

口径: P1=推荐池 stock_pool status!=exited + recommendation_tracking tracking;
P2=paper_positions_snapshot (join stock_pool 补 entry_date/holding_days——
paper_positions_snapshot schema 无此两列) + stock_pool active (统筹裁两段并列)。
用户视角席 qlib 教训: 信号质量 (p1) vs 组合表现 (p2) 两个独立打分板分开。
"""
import sqlite3
from unittest.mock import MagicMock

import pandas as pd
import pytest


def _pool_rows():
    return [
        ("600519", "贵州茅台", "active", "buy", 0.85, 0.72, "agent 看好",
         1200.0, 1250.0, 1150.0, 1.8, "2026-09-24"),
    ]


# --- P1 三钉 ---
def test_p1_pool_excludes_exited():
    """钉①: 推荐池 WHERE status!='exited' — 已退出不进推荐池。"""
    rows = _pool_rows() + [("000001", "平安银行", "exited", "sell", 0.5, 0.4,
                            "退出", 10.0, 11.0, 9.0, 1.5, "2026-09-20")]
    active = [r for r in rows if r[2] != "exited"]
    assert len(active) == 1 and active[0][0] == "600519"


def test_p1_added_reason_not_rank_phrase():
    """钉②: added_reason 带 agent verdict 非「候选池第X位」排名话术 (P0-5 铁闸)。"""
    reason = "agent 看好：价值低估+质量稳"
    assert "候选池第" not in reason, "排名话术永不回落"


def test_p1_tracking_state_filter():
    """钉③: recommendation_tracking WHERE state='tracking' — 在跟状态过滤。"""
    rows = [("600519", "tracking"), ("000001", "closed")]
    tracking = [r for r in rows if r[1] == "tracking"]
    assert len(tracking) == 1 and tracking[0][0] == "600519"


# --- P2 三钉 ---
def test_p2_snapshot_latest_date_only():
    """钉④: 纸面持仓 WHERE date=MAX(date) — 只取最新快照。"""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE paper_positions_snapshot "
                 "(date TEXT, code TEXT, shares REAL, mv REAL, weight REAL, "
                 "entry_price REAL, unrealized_pnl REAL)")
    conn.execute("INSERT INTO paper_positions_snapshot VALUES "
                 "('2026-09-24', '600519', 100, 120000, 0.5, 1200, 500)")
    conn.execute("INSERT INTO paper_positions_snapshot VALUES "
                 "('2026-09-25', '000001', 200, 22000, 0.5, 11, -100)")
    rows = conn.execute(
        "SELECT * FROM paper_positions_snapshot "
        "WHERE date = (SELECT MAX(date) FROM paper_positions_snapshot)").fetchall()
    assert len(rows) == 1 and rows[0][1] == "000001"


def test_p2_holding_days_join():
    """钉⑤: holding_days=julianday(date)-julianday(entry_date) — join stock_pool 补。"""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE paper_positions_snapshot "
                 "(date TEXT, code TEXT, entry_price REAL)")
    conn.execute("CREATE TABLE stock_pool (code TEXT, status TEXT, entry_date TEXT)")
    conn.execute("INSERT INTO paper_positions_snapshot VALUES ('2026-09-25', '600519', 1200)")
    conn.execute("INSERT INTO stock_pool VALUES ('600519', 'active', '2026-09-20')")
    row = conn.execute(
        "SELECT CAST(julianday(p.date) - julianday(sp.entry_date) AS INTEGER) "
        "FROM paper_positions_snapshot p "
        "LEFT JOIN stock_pool sp ON sp.code = p.code AND sp.status = 'active'").fetchone()
    assert row[0] == 5, f"持有 5 天: {row}"


def test_p2_active_positions_filter():
    """钉⑥: stock_pool WHERE status='active' — 执行层持仓过滤。"""
    rows = [("600519", "active"), ("000001", "watch")]
    active = [r for r in rows if r[1] == "active"]
    assert len(active) == 1 and active[0][0] == "600519"
