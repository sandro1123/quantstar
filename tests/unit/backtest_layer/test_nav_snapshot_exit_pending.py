# -*- coding: utf-8 -*-
"""Day 5 卡E TDD — settlement.py snapshot_positions_for_date 镜像包A (派单 sm-695aabac1b9640bd)

病: settlement.py:43 `WHERE status = 'active'` 与 R-5 包A 同根 —
stock_pool.status 值域实测 = ['exit_pending','exited','pending_entry','watch']，
'active' 从未写入（T+1 结算当日转 exit_pending），生产 paper_positions_snapshot
max(date)=09-25 后 12 天零新增。

修法（镜像包A）: status 筛选改 IN ('active','exit_pending','pending_entry')。
反向钉: ①exit_pending 25 只须进快照 ②对账容差 1%（Σmv 对齐 pnl_tracker 分子）。
判例 #148 先量后写（统筹 10-07 实测 max=09-25，本钉复刻）;判例 #143 值域枚举。
"""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace


_STOCK_POOL_DDL = """
CREATE TABLE IF NOT EXISTS stock_pool (
    code TEXT PRIMARY KEY, name TEXT, status TEXT,
    entry_price REAL, entry_date TEXT, position_qty INTEGER,
    last_price REAL, last_price_date TEXT,
    unrealized_pnl REAL DEFAULT 0,
    unrealized_pnl_pct REAL DEFAULT 0,
    exit_price REAL, exit_date TEXT, exit_reason TEXT,
    added_date TEXT, updated_at TEXT
)
"""

_SNAPSHOT_DDL = """
CREATE TABLE IF NOT EXISTS paper_positions_snapshot (
    date TEXT NOT NULL,
    code TEXT NOT NULL,
    shares INTEGER,
    mv REAL,
    weight REAL,
    entry_price REAL,
    unrealized_pnl REAL,
    created_at TEXT DEFAULT (datetime('now', '+8 hours')),
    PRIMARY KEY (date, code)
)
"""


def _fixture_db(exit_pending_rows: list[tuple], active_rows: list[tuple] = ()):
    """构造夹具: (code, position_qty, last_price) 行。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(_STOCK_POOL_DDL)
    conn.execute(_SNAPSHOT_DDL)
    for code, qty, price in exit_pending_rows:
        conn.execute(
            "INSERT INTO stock_pool (code, name, status, entry_price, position_qty,"
            " last_price, last_price_date, added_date)"
            " VALUES (?, 'X', 'exit_pending', ?, ?, ?, '2026-09-18', '2026-09-18')",
            (code, price, qty, price))
    for code, qty, price in active_rows:
        conn.execute(
            "INSERT INTO stock_pool (code, name, status, entry_price, position_qty,"
            " last_price, last_price_date, added_date)"
            " VALUES (?, 'A', 'active', ?, ?, ?, '2026-10-04', '2026-10-04')",
            (code, price, qty, price))
    conn.commit()
    return conn


class TestSnapshotCoversExitPending:
    """反向钉①: exit_pending 行必进快照（镜像包A R-5 分子集合）。"""

    def test_exit_pending_rows_enter_snapshot(self):
        from backtest_layer.paper_trading.settlement import snapshot_positions_for_date
        rows = [(f"ep{i:02d}", 100 + i * 15, 10.0 + i * 0.5) for i in range(25)]
        conn = _fixture_db(exit_pending_rows=rows)
        db = SimpleNamespace(get_connection=lambda: conn)
        out = snapshot_positions_for_date(db, "2026-10-04")
        assert out.get("rows") == 25, (
            f"exit_pending 25 只须进快照 (镜像包A); 实得 rows={out.get('rows')}, "
            f"skipped={out.get('skipped')}, out={out}")
        n = conn.execute(
            "SELECT COUNT(*) FROM paper_positions_snapshot WHERE date='2026-10-04'").fetchone()[0]
        assert n == 25, f"paper_positions_snapshot 应落 25 行; 实得 {n}"

    def test_active_rows_still_enter_snapshot(self):
        """正向钉: active 行（若真出现）不回退照进。"""
        from backtest_layer.paper_trading.settlement import snapshot_positions_for_date
        conn = _fixture_db(exit_pending_rows=[], active_rows=[("act01", 100, 10.0)])
        db = SimpleNamespace(get_connection=lambda: conn)
        out = snapshot_positions_for_date(db, "2026-10-04")
        assert out.get("rows") == 1, f"active 行应照进快照; 实得 {out}"


class TestSnapshotReconcilesWithPnlTracker:
    """反向钉②: Σmv 对账容差 1%（与 pnl_tracker 同分子口径）。"""

    def test_snapshot_mv_sum_matches_position_value_within_1pct(self):
        from backtest_layer.paper_trading.settlement import snapshot_positions_for_date
        rows = [(f"ep{i:02d}", 100 + i * 15, 10.0 + i * 0.5) for i in range(25)]
        conn = _fixture_db(exit_pending_rows=rows)
        db = SimpleNamespace(get_connection=lambda: conn)
        snapshot_positions_for_date(db, "2026-10-04")
        snap_mv = conn.execute(
            "SELECT COALESCE(SUM(mv), 0) FROM paper_positions_snapshot"
            " WHERE date='2026-10-04'").fetchone()[0]
        pool_mv = conn.execute(
            "SELECT COALESCE(SUM(last_price * position_qty), 0) FROM stock_pool"
            " WHERE status IN ('exit_pending')").fetchone()[0]
        assert pool_mv > 0
        diff = abs(snap_mv - pool_mv) / pool_mv
        assert diff < 0.01, (
            f"Σmv 对账容差 1% 破: snap={snap_mv} vs pool={pool_mv} (diff={diff:.4%})")

    def test_missing_price_rows_skipped_not_faked(self):
        """缺价票 skip 留痕不造假 mv=0 行（原降级语义保持）。"""
        from backtest_layer.paper_trading.settlement import snapshot_positions_for_date
        conn = _fixture_db(exit_pending_rows=[("ep00", 100, 10.0), ("ep01", 200, None)])
        db = SimpleNamespace(get_connection=lambda: conn)
        out = snapshot_positions_for_date(db, "2026-10-04")
        assert out.get("rows") == 1 and out.get("skipped") == 1, (
            f"缺价票应 skip; 实得 {out}")
