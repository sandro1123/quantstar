"""EX-3: paper_positions_snapshot 日终落表 — TDD 测试 (红先行)。

缺口 (期4 素材取证): stock_pool 是"活仓现值表" (每行 = 一只票最新态),
无按日历史 — 持仓权重/市值的时间序列无处可查, 期4 四个半通电面之一
(weights 空)。日终 (fx_settlement 18:05 窗) 落一张快照:
date + code + shares + mv + weight + entry_price + unrealized_pnl。

幂等: PK (date, code), 重跑 INSERT OR REPLACE 覆盖当日快照 (日终价
可能晚到, 最后一跑为准)。权重 = 该仓 mv / Σ(active mv) (现金仓不进表,
weight 是仓位内占比; NAV 口径另在 paper_trading_stats)。
降级: 无活仓 → 零行快照 (诚实空, 不造假行); K 线缺 → mv 用 last_price
兜底, 仍缺则跳过该票留痕。
"""
import sqlite3

import pytest

from backtest_layer.paper_trading.settlement import snapshot_positions_for_date
from execution_layer.tables import init_execution_tables


class _Db:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row

    def get_connection(self):
        return self.conn


def _db_with_positions(active_rows, date="2026-09-10"):
    """执行层真实 DDL + stock_pool active 行 (code/qty/entry/last_price)"""
    db = _Db()
    init_execution_tables(db.conn)
    for r in active_rows:
        db.conn.execute(
            "INSERT INTO stock_pool (code, name, status, fund_type, added_date, position_qty, "
            "entry_price, last_price, last_price_date, unrealized_pnl_pct, unrealized_pnl) "
            "VALUES (?, ?, 'active', 'simulated', ?, ?, ?, ?, ?, ?, ?)",
            (r["code"], r.get("name", r["code"]), date, r["qty"], r["entry"],
             r.get("last", r["entry"]), date if r.get("last") else None, r.get("pnl_pct"), r.get("pnl_abs")))
    db.conn.commit()
    return db


ACTIVE_2 = [
    {"code": "600980", "qty": 2200, "entry": 19.54, "last": 19.28, "pnl_pct": -1.33},
    {"code": "603045", "qty": 700, "entry": 49.98, "last": 50.48, "pnl_pct": 1.0},
]


class TestSnapshotSchema:
    def test_table_created_by_init(self):
        db = _Db()
        init_execution_tables(db.conn)
        cols = [r[1] for r in db.conn.execute(
            "PRAGMA table_info(paper_positions_snapshot)").fetchall()]
        for c in ("date", "code", "shares", "mv", "weight", "entry_price", "unrealized_pnl"):
            assert c in cols, f"列 {c} 缺失"


class TestSnapshotDaily:
    def test_two_positions_written(self):
        db = _db_with_positions(ACTIVE_2)
        result = snapshot_positions_for_date(db, "2026-09-10")
        assert result["rows"] == 2
        rows = db.conn.execute(
            "SELECT code, shares, mv, weight, entry_price, unrealized_pnl "
            "FROM paper_positions_snapshot WHERE date='2026-09-10' ORDER BY code").fetchall()
        assert [r["code"] for r in rows] == ["600980", "603045"]
        r0, r1 = rows
        assert r0["shares"] == 2200
        assert r0["mv"] == round(19.28 * 2200, 2)
        assert r1["mv"] == round(50.48 * 700, 2)
        total = 19.28 * 2200 + 50.48 * 700
        assert r0["weight"] == round(19.28 * 2200 / total, 4)
        assert r1["weight"] == round(50.48 * 700 / total, 4)
        assert r0["entry_price"] == 19.54
        assert r0["unrealized_pnl"] == pytest.approx((19.28 - 19.54) * 2200, abs=0.01)

    def test_idempotent_same_day_rerun_replaces(self):
        db = _db_with_positions(ACTIVE_2)
        snapshot_positions_for_date(db, "2026-09-10")
        # 次日价晚到, last_price 更新后重跑 → 当日快照被覆盖, 不双行
        db.conn.execute("UPDATE stock_pool SET last_price=20.0 WHERE code='600980'")
        db.conn.commit()
        result = snapshot_positions_for_date(db, "2026-09-10")
        assert result["rows"] == 2
        n = db.conn.execute(
            "SELECT COUNT(*) FROM paper_positions_snapshot WHERE date='2026-09-10'").fetchone()[0]
        assert n == 2
        mv = db.conn.execute(
            "SELECT mv FROM paper_positions_snapshot WHERE date='2026-09-10' AND code='600980'").fetchone()
        assert mv["mv"] == round(20.0 * 2200, 2)

    def test_no_active_positions_gives_empty_honest(self):
        db = _db_with_positions([])
        result = snapshot_positions_for_date(db, "2026-09-10")
        assert result["rows"] == 0
        n = db.conn.execute(
            "SELECT COUNT(*) FROM paper_positions_snapshot WHERE date='2026-09-10'").fetchone()[0]
        assert n == 0

    def test_missing_last_price_skips_row(self):
        """last_price 缺 (从未盯市) → 该票跳过留痕, 不造 mv=0 假行"""
        rows = [{"code": "000001", "qty": 100, "entry": 10.0, "last": None}]
        db = _db_with_positions(rows)
        result = snapshot_positions_for_date(db, "2026-09-10")
        assert result["rows"] == 0
        assert result["skipped"] == 1


class TestSnapshotWiring:
    def test_settlement_entry_calls_both_legs(self):
        """接线守卫: settle_daily 总入口同时挂 benchmark + 快照两腿"""
        import inspect
        from backtest_layer.paper_trading.settlement import settle_daily
        src = inspect.getsource(settle_daily)
        assert "settle_benchmark_for_date" in src, "benchmark 腿未挂进 settle_daily"
        assert "snapshot_positions_for_date" in src, "快照腿未挂进 settle_daily"

    def test_fx_settlement_handler_wires_both_legs(self):
        import inspect
        from ops_layer.orchestrator import PipelineOrchestrator
        src = inspect.getsource(PipelineOrchestrator._settle_benchmark_leg)
        assert "settle_daily" in src, "日终结算总入口未接 fx_settlement"
        # fx_settlement 三出口守卫 (EX-1 已立, 维持)
        fx_src = inspect.getsource(PipelineOrchestrator._run_fx_settlement)
        assert "_settle_benchmark_leg" in fx_src, "结算腿未接 fx_settlement 三出口"
