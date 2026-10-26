# -*- coding: utf-8 -*-
"""Day 4 包B 反向钉： 生产链路自动赋值 `_last_curve_end`（不 手 动 注 入）

判 例 #150 反 伪 真： 生产代码 `_last_curve_end` 零赋值点 = 手 动 注 入 伪 真。
反向钉： 用 假 PnLTracker 让 _current_drawdown 在 真实 db + stock_pool 上 跑, 且
curve 末端 已 6 天 前 → _curve_stale_days() > 5 → _stale_nav_reason() 必 显 式
陈 旧； curve 末端 今 日 → 空串。 不 手 动 注 入 `_last_curve_end` — 这 是
「 生产链路 应 自动运行 的 证 明」, 与 测 试 手 动 注 入 的 区 分。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace

from common.utils import bj_date


def _gate_db_with_stock(rows: list[tuple]):
    """Build ProtectionGate with in-memory stock_pool rows:(code,status,last_price,last_price_date,position_qty)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE stock_pool (code TEXT, status TEXT, last_price REAL, last_price_date TEXT, position_qty INTEGER,"
        " entry_price REAL, entry_date TEXT, unrealized_pnl_pct REAL, exit_price REAL)")
    for r in rows:
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


class TestProductionChainAutoAssignsLastCurveEnd:
    """反向钉： 生产外链 应 自动 给 `_last_curve_end` 赋值, 不 依 赖 手 动 注 入"""

    def test_stale_curve_end_triggers_stale_reason_via_production_chain(self):
        today = bj_date()
        stale = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=6)).strftime("%Y-%m-%d")
        gate = _gate_db_with_stock([("x", "active", 10.0, stale, 100)])
        dd = gate._current_drawdown()
        assert dd is None or dd <= 0.01, (
            f"陈旧曲线末端应 None 或近 0；实得 {dd}（可用护航")
        reason = gate._stale_nav_reason()
        assert "陈旧" in reason and "放行" in reason, (
            f"生产链 应 truthy 陈 旧；实得: {reason}")

    def test_fresh_curve_end_today_no_stale_reason_via_production_chain(self):
        today = bj_date()
        gate = _gate_db_with_stock([("x", "active", 10.0, today, 100)])
        dd = gate._current_drawdown()
        assert dd is None or dd <= 0.01, (
            f"active=0 应 None 或近 0 (milli-README.实得 {dd}")
        reason = gate._stale_nav_reason()
        assert reason == "", f"新 鲜 应 空 串；实得: {reason}"
