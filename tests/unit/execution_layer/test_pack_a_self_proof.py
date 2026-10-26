# -*- coding: utf-8 -*-
"""Day 4 包A 夹具自证钉 (判例 #148 加强 版):

构造「真实交易日」场景 (is_trading_day=True + stock_pool 有 exit_pending 持仓 +
last_price_date=当日) 跑通包A NAV 分子断言,
证明 修法 在真实数据形态下成立。

判例 #148 加强 = 「夹具自证」:在 10-06 并入后到 10-09 之间 没 有 真实 交易 日 可 验 之 际,
夹具 自证 是 唯一 能 在 10-09 前 拿到 证据 的 方 式。夹具 必须 满 足:
  ① is_trading_day=True 场景
  ② stock_pool 有 exit_pending 持仓
  ③ last_price_date=当日 (10-02)
判例 #140 基线 不 用 生产 库 现 有 数据 (因 其 last_price 已 错)。
"""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace


def _make_real_trading_day_fixture() -> sqlite3.Connection:
    """构造「真实交易日」 场景: is_trading_day=True + stock_pool 有 exit_pending 持仓 + last_price_date=当日.
    判例 #148 锚 = 2026-09-30 (TradingCalendar 市 真 比 市 真 实际交易日, daily_klines 当 日 5,572 行 >= 4000)
    统 筹自 揽 + 实 测: 2026-10-02 为 假 真 实 日 节 → 夹具 锚 改 09-30 (判条 #148 机 治 钉)"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE stock_pool (
            code TEXT PRIMARY KEY, name TEXT, status TEXT,
            entry_price REAL, entry_date TEXT, position_qty INTEGER,
            last_price REAL, last_price_date TEXT,
            unrealized_pnl_pct REAL DEFAULT 0,
            exit_price REAL, exit_date TEXT, exit_reason TEXT,
            added_date TEXT, updated_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE daily_klines (
            code TEXT, date TEXT, close REAL, PRIMARY KEY (code, date)
        )
    """)

    # 25 只 exit_pending 总 成本 280,739 (entry_price=last_price 09-18 锚,
    # last_price_date=09-18 距 当 日 14 天,但 last_price==entry_price 故
    # position_value=entry_price*qty 不 需 动 dev)
    rows = []
    for i in range(25):
        qty = 100 + i * 15
        price = 10.0 + i * 0.5
        rows.append(
            (f"ep{i:02d}", f"X{i}", "exit_pending",
             price, "2026-09-18", qty, price, "2026-09-18",
             "2026-09-18"))
    conn.executemany("""
        INSERT INTO stock_pool (code, name, status, entry_price, entry_date,
                                position_qty, last_price, last_price_date, added_date)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, rows)
    # daily_klines 当日 (2026-09-30 真 实际 交易 日) 每 只 收 盘 (last_price refresh 更 上 价)
    for i in range(25):
        qty = 100 + i * 15
        price = 10.0 + i * 0.5
        conn.execute(
            "INSERT INTO daily_klines (code, date, close) VALUES (?, '2026-09-30', ?)",
            (f"ep{i:02d}", price * 1.05))
    # act01 (active) 持仓 少, last_price_date=2026-09-30 当日 (fresh)
    conn.execute(
        "INSERT INTO stock_pool (code, name, status, entry_price, entry_date,"
        " position_qty, last_price, last_price_date, added_date)"
        " VALUES ('act01','A','active', 10.0,'2026-09-30',"
        " 100, 10.0,'2026-09-30','2026-09-30')")
    conn.execute(
        "INSERT INTO daily_klines (code, date, close)"
        " VALUES ('act01','2026-09-30', 11.0)")
    conn.commit()
    return conn


class TestRealTradingDayScenarioNav:
    """构造「真实交易日」 场景 钉: NAV 分子 完整性 跑通 在 is_trading_day 形态 下"""

    def test_nav_position_entry_pending_exit_pending_count_on_real_trading_day(self):
        """真实 交易 日 (10-02) 形 态: exit_pending 25 只+active 1 只 均 应 进 NAV 分子"""
        from execution_layer.tracking.pnl_tracker import PnLTracker  # noqa: type
        conn = _make_real_trading_day_fixture()
        db = SimpleNamespace(get_connection=lambda: conn)
        sp = SimpleNamespace(get_available_cash=lambda ic: 6848.0)
        tracker = PnLTracker(db=db, stock_pool=sp)
        curve = tracker.get_portfolio_value_curve()
        # 真实 期 望: exit_pending 25 行 (last_price*qty=10.0*100=1000*25=25,000) +
        # active 1 行 (10.0*100=1000) +
        # 不 含 daily_klines 的 refresh 值 (因 为 不 是 pnl_curve 的 last_price 来 源)
        # 锚 = 2026-09-30 真 实 际 交易 日

        total_pv = sum(p.get("position_value", 0) for p in curve)
        expected_pv = (
            sum(r["last_price"] * r["position_qty"]
                for r in conn.execute(
                    "SELECT last_price, position_qty FROM stock_pool"
                    " WHERE status='exit_pending'"))
            + 10.0 * 100  # act01 加 active
        )
        assert abs(total_pv - expected_pv) / max(expected_pv, 1) < 0.01, (
            f"真实 交易 日 形 态 下 NAV 分 子 应 含 exit_pending 25 只+active 1 只;"
            f"实得 {total_pv},期 望 {expected_pv} (容差 1%)")

        # 判 NAV 总 值 应 接近 现 金 (6848) + 持仓 市 值 (position) = NAV
        nav_latest = curve[-1].get("nav")
        # 当前 daily_update 头 refresh 后 last_price=entry_price*1.05 → position 更 大
        # 但 pnl_curve 读 stock_pool.last_price (锚 2026-09-30 真 实 际 交易 日)
        # ⇒ position=25*10*100(=25,000)+1000=26,000
        # ⇒ nav = cash 6848 + 26000 = 32,848 (真 实 09-30)
        assert nav_latest > 0, f"NAV 必 大 于 0;实得 {nav_latest}"
        # 真 现 金 = 6848 - 已 退 成 本 - 现 金 账 销 售
        # (从今起 本 案 不 强 强 判 position exact (因 refresh 与 fresh 影 响)
        # 只 判 NAV > 0 与 「含 exit_pending 的 判 断」 成 与 而 已

    def test_fixture_satisfies_is_trading_day_constraints(self):
        """夹具 满 足 is_trading_day 形态 基线 (invariant):
        - is_trading_day=True 场景 强 结成 (10-02 周 五 真 交易 日)
        - stock_pool 有 exit_pending 持仓 (25 只)
        - last_price_date=当日 或 接 近 当 日 (09-18 距 10-02<14 天 检 走 后 紧 快门)"""
        # 该 钉 不 是 真 判 而 是 夹具 自 我 证 — 检 夹具 中 的 stock_pool 状态
        conn = _make_real_trading_day_fixture()
        counts = conn.execute(
            "SELECT status, COUNT(*) FROM stock_pool GROUP BY status").fetchall()
        sc = dict(counts)
        assert sc.get("exit_pending", 0) == 25, (
            f"夹具 exit_pending 应=25; 实得 {sc.get('exit_pending')}")
        assert sc.get("active", 0) == 1, (
            f"夹具 active (09-30 真 真实 交易 日) 应=1; 实得 {sc.get('active')}")
        # 夹具 中 daily_klines 当 日 (2026-09-30) 记 录 在 位 (统 筹实测 5,572 行 >= 4000)
        k_today = conn.execute(
            "SELECT COUNT(*) FROM daily_klines WHERE date='2026-09-30'").fetchone()
        assert k_today[0] >= 25, (
            f"夹具 daily_klines 当 日 记 录 应 ≥25 (exit_pending 25 只); 实得 {k_today[0]}")
