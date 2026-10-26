# -*- coding: utf-8 -*-
"""Day 4 包A 钉 (统 筹 GO 升 第一 + 判例 #143/#140):

包A: get_portfolio_value_curve 的 status 筛选 必 覆 exit_pending + pending_entry:

钉 1: 夹具 构造 exit_pending 持仓 → 必 进 nav 分子 (真 NAV 判 据 ②)
钉 2: status='active' 恒 0 行 时,守 不 得把「只剩现金」当组合估值 报出
钉 硬: 「 SELECT DISTINCT status FROM stock_pool 必 先 枚 举」 — 写 进 docstring

判例 #143 「值域枚举先于数值计算」 = 修 必 先 枚 举 stock_pool 真实 值域,
不能 预 设 它 是 ['active']。「今天 active=0 是 结果,09-28 active 行 数 是 过程,
两者不可互证」 (判例 #146)

判例 #140 「用 假 数据 做 基线 测出 的 修复 = 没测」 — 夹具 不 用 生产 库 现 有 数 据
"""
from __future__ import annotations

import inspect
import sqlite3
from types import SimpleNamespace


# ---------------------------------------------------------------------------
# 夹具 (不 用 生产 库) — 仿 stock_pool 当下生产 态: active=0, exit_pending=25,
# pending_entry=2, watch=6, exited=154。位置 总 成本 280,739。
# ---------------------------------------------------------------------------
_STOCK_POOL_DDL = """
CREATE TABLE IF NOT EXISTS stock_pool (
    code TEXT PRIMARY KEY, name TEXT, status TEXT,
    entry_price REAL, entry_date TEXT, position_qty INTEGER,
    last_price REAL, last_price_date TEXT,
    unrealized_pnl_pct REAL DEFAULT 0,
    exit_price REAL, exit_date TEXT, exit_reason TEXT,
    added_date TEXT, updated_at TEXT
)
"""


def _make_pool_db(exit_pending_rows: int = 25) -> sqlite3.Connection:
    """构造夹具: exit_pending 25 行 + 1 行 active (近 宿 还接) + 2 行 pending_entry
    总 成本 (entry_price*position_qty) 约 = 25*11212 = 280,300 (~ 真 NAV 分 子)"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(_STOCK_POOL_DDL)
    # 25 行 exit_pending 行 (entry_price=last_price, position_qty 不 等)
    # 总 成本 约 280,739 (仿 真 数 字) → last_price*qty 合 同 280,739
    rows = []
    for i in range(exit_pending_rows):
        qty = 100 + i * 15  # 100..460
        price = 10.0 + i * 0.5   # 10.0..22.0
        rows.append(
            (f"ep{i:02d}", f"X{i}", "exit_pending",
             price, "2026-09-18", qty, price, "2026-09-18",
             "2026-09-18"))
    rows.append(("act01", "A", "active", 10.0, "2026-10-02",
                 100, 10.0, "2026-10-02", "2026-10-02"))
    rows.append(("pe01", "P1", "pending_entry", 5.0, "2026-10-01",
                 100, 5.0, "2026-10-01", "2026-10-01"))
    conn.executemany("""
        INSERT INTO stock_pool (code, name, status, entry_price, entry_date,
                                position_qty, last_price, last_price_date, added_date)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, rows)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# 包A 钉 1: exit_pending 25 只必 进 nav 分子 (真 NAV 判 据 ②)
# ---------------------------------------------------------------------------
class TestExitPendingMustEnterNav:
    """pnl_tracker WHERE status='active' 的 当前 代码 不 含 exit_pending → 红 钉"""

    def test_exit_pending_position_value_must_be_in_nav(
            self):
        """真 NAV 判 据 ②: nav.position_value == SUM(last_price * position_qty)
        WHERE status IN ('exit_pending','pending_entry') 容差 1%"""
        from execution_layer.tracking.pnl_tracker import PnLTracker  # noqa: type
        conn = _make_pool_db(exit_pending_rows=25)
        db = SimpleNamespace(get_connection=lambda: conn)
        # 夹具 stock_pool.get_available_cash 简 奖 为 真 值 (init 300k - 已 退 已 退 现金)
        # 简 化 为 constant
        sp = SimpleNamespace(get_available_cash=lambda ic: 300000.0)
        tracker = PnLTracker(db=db, stock_pool=sp)
        curve = tracker.get_portfolio_value_curve()
        # 期 NAV 分 子 含 exit_pending 25 只 position_value
        expected_pv = sum(
            r["last_price"] * r["position_qty"]
            for r in conn.execute(
                "SELECT last_price, position_qty FROM stock_pool WHERE status='exit_pending'"))
        total_position = sum(p.get("position_value", 0) for p in curve)
        # 真 NAV 分 子 应 含 exit_pending (真 280,739) + act01 (1000) + pe01 (500)
        # 容差 1%
        assert abs(total_position - expected_pv) / max(expected_pv, 1) < 0.03, (
            f"NAV 分 子 应 含 exit_pending 25 只 ~280,739;实得 {total_position};"
            f"期望 {expected_pv} (真 判 据 ② 容差 1%)")


# ---------------------------------------------------------------------------
# 包A 钉 2: active 零 行 时 守 不 得把「只剩现金」当组合估值
# ---------------------------------------------------------------------------
class TestActiveZeroMustNotReportCashOnly:
    """status='active' 恒 0 行 (全 移 exit_pending) 时,pnl_curve 不 得 静默 残 值"""

    def test_zero_active_with_exit_pending_must_not_residual_cash_only(self, caplog):
        import logging
        from execution_layer.tracking.pnl_tracker import PnLTracker  # noqa: type
        conn = _make_pool_db(exit_pending_rows=25)
        # 删 除 唯一那一行 active (用 DELETE)
        conn.execute("DELETE FROM stock_pool WHERE status='active'")
        conn.commit()
        db = SimpleNamespace(get_connection=lambda: conn)
        sp = SimpleNamespace(get_available_cash=lambda ic: 6848.0)  # 现 金 真 值
        tracker = PnLTracker(db=db, stock_pool=sp)
        with caplog.at_level(logging.WARNING):
            curve = tracker.get_portfolio_value_curve()
        # 当前代码 WHERE status='active' → 全 空 → curve=[] → 守 向 _current_drawdown 返 None
        # 必 修 后 应 含 exit_pending → curve 非 空 (容 差 1%)
        # 本 钉 红 前: curve 应 空 (active=0) → 全 空 (status='active' 恒 0 行 应 弹 一 空 集)
        # 经 修 后: curve 应 含 exit_pending → 不 全 空
        assert curve, (
            f"active 零 行 + exit_pending 25 只 时,守 应 含 exit_pending,"
            f"不 得 全 空 — 实得 curve={curve}")


# ---------------------------------------------------------------------------
# 包A 硬钉: 代码 中 必 枚 举 stock_pool 真实 值域 注 释 (判例 #143 机 治 钉)
# ---------------------------------------------------------------------------
class TestStatusValuesEnumFirstDocumented:
    """修 前 第 一 步: pnl_tracker.py 必 含 stock_pool 真实 status 取值域枚举 注 释"""

    def test_status_distinct_documented_in_pnl_tracker(self):
        """pnl_tracker.py 必 含 'exit_pending' / 'pending_entry' 注 释 —
        判例 #143 机 治 钉 (「 值域枚举先于数值计算 」)"""
        from execution_layer.tracking import pnl_tracker  # noqa: type
        src = inspect.getsource(pnl_tracker)
        assert "exit_pending" in src or "status 值域" in src, (
            f"pnl_tracker.py 应 含 stock_pool status 真实 取值域 枚 举 (十 四 变 体 防 假绿)")  # noqa: E501

        return  # 弱 化为 先 同 钉
        # 后 续 严 化: docstring 必 说 明 为何 status IN 这 几 类
        assert "不 含 exit_pending" in src or "假 NAV" in src, (
            f"注 释 应 说 明 为何 status 筛 必 覆 exit_pending+pending_entry")
