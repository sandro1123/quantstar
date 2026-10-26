# -*- coding: utf-8 -*-
"""Day 4 包B/包E 钉 — 守卫读数陈旧 / active=0 显式放行 reason

判例 #148 先量后写：实测 main(cb853a12) 生产 NAV 曲线末端 09-25，距 10-04 为 9 天
(>5 自然日，对齐包C 闩锁时效量级)，navs[-1] 为冻结点非现值。
判例 #138/#153：显式 reason 非空串（陈旧 >5 给「陈旧」；active=0 给「无活跃持仓显式放行」）。
判例 #150：锚取 stock_pool.last_price_date（与 pnl_curve 同 SQL 过滤）。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace

from common.utils import bj_date


def _gate_with_stock(stock_rows: list[tuple]):
    """Build ProtectionGate with in-memory stock_pool rows: (code,status,last_price,last_price_date,position_qty)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE stock_pool (code TEXT, status TEXT, last_price REAL, last_price_date TEXT, position_qty INTEGER)")
    for r in stock_rows:
        conn.execute(
            "INSERT INTO stock_pool (code,status,last_price,last_price_date,position_qty) VALUES (?,?,?,?,?)", r)
    conn.commit()
    db = SimpleNamespace(get_connection=lambda: conn)
    sp = SimpleNamespace(get_available_cash=lambda ic: 300000.0)
    sp.action_record = SimpleNamespace(write=lambda **kw: None)
    from execution_layer.core import protection_gate as pg
    gate = pg.ProtectionGate.__new__(pg.ProtectionGate)
    gate.db = db
    gate.stock_pool = sp
    gate.config = None
    return gate


class TestStaleNavReasonExplicit:
    def test_curve_end_6_days_ago_stale_reason(self):
        today = bj_date()
        stale = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=6)).strftime("%Y-%m-%d")
        gate = _gate_with_stock([("x", "active", 10.0, stale, 100)])
        gate._last_curve_end = stale
        reason = gate._stale_nav_reason()
        assert "守卫读数陈旧" in reason and "显式放行" in reason, f"实得: {reason}"

    def test_curve_end_today_fresh_no_reason(self):
        today = bj_date()
        gate = _gate_with_stock([("x", "active", 10.0, today, 100)])
        gate._last_curve_end = today
        reason = gate._stale_nav_reason()
        assert reason == "", f"新 鲜 应 空 串；实 得: {reason}"

    def test_no_active_returns_empty_string(self):
        today = bj_date()
        gate = _gate_with_stock([("x", "exit_pending", 10.0, today, 100)])
        reason = gate._stale_nav_reason()
        assert reason == "", f"无 active 时 现 行 应 返 空 串；实 得: {reason}"


class TestCheckDrawdownExplicit:
    def test_active_zero_dd_none_returns_explicit_pass(self):
        today = bj_date()
        gate = _gate_with_stock([("x", "exit_pending", 10.0, today, 100)])
        result = gate._check_drawdown("600000")
        assert "无 活 跃 持 仓" in result and "显式放行" in result, (
            f"active=0 时应显式放行；实得: {result}")
