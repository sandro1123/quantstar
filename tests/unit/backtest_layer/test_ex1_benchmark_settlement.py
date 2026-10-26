"""EX-1: benchmark 日终结算线 — TDD 测试 (红先行)。

病根 (2026-09-10 取证): D8 (324c2df, 08-27) 的 16:35 reconcile 路径在生产
13 天从未落下过一个 benchmark_return 值 (PTS 十行全 NULL), 而同代码同库手动
复跑非 None — 病在 16:35 cron 时序环境, 不在计算逻辑。工程裁定: 不再复现
16:35 竞态, 改挂 18:05 fx_settlement 日终结算腿 (彼时 index_daily 当日行
100% 已在 — ddp 15:47~15:59 子管道 + 17:40 kline 兜底双保险), UPDATE 当日
PTS 行, 行不存在则 INSERT 兜底 (16:35 锁堵 skipped 的日子也能有基准)。

口径 (与 D8 一致): 当日收益 = close(T)/close(T-1) - 1。指数缺数/无表 →
不写假值, 降级留痕 (铁律 #12)。
"""
import sqlite3
from types import SimpleNamespace

import pytest

from backtest_layer.paper_trading.settlement import settle_benchmark_for_date
from execution_layer.tables import init_execution_tables


class _Db:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row

    def get_connection(self):
        return self.conn


class _DL:
    """DL-23 契约假数据层: 三日指数 (T-2, T-1, T)"""

    def __init__(self, rows=None):
        self._rows = rows if rows is not None else [
            {"date": "2026-09-08", "close": 4558.737},
            {"date": "2026-09-09", "close": 4600.0},
            {"date": "2026-09-10", "close": 4570.0},
        ]

    def get_index_daily(self, code, start_date="", end_date=""):
        return [r for r in self._rows
                if (not start_date or r["date"] >= start_date)
                and (not end_date or r["date"] <= end_date)]


PTS_DDL = """
CREATE TABLE IF NOT EXISTS paper_trading_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    total_signals INTEGER,
    filled_signals INTEGER,
    avg_slippage REAL,
    max_slippage REAL,
    paper_return REAL,
    benchmark_return REAL,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
)
"""


def _pts_db(date="2026-09-10", benchmark=None, with_row=True):
    db = _Db()
    db.conn.executescript(PTS_DDL)
    if with_row:
        db.conn.execute(
            "INSERT INTO paper_trading_stats "
            "(date, total_signals, filled_signals, avg_slippage, max_slippage, paper_return, benchmark_return) "
            "VALUES (?, 10, 10, 0.001, 0.005, -0.00304, ?)",
            (date, benchmark))
        db.conn.commit()
    return db


class TestSettleBenchmarkUpdate:
    """修法①主路径: 当日 PTS 行存在 → UPDATE benchmark_return"""

    def test_updates_existing_row(self):
        db = _pts_db(benchmark=None)
        result = settle_benchmark_for_date(db, _DL(), "2026-09-10")
        assert result["written"] is True
        row = db.conn.execute(
            "SELECT benchmark_return FROM paper_trading_stats WHERE date='2026-09-10'").fetchone()
        assert row["benchmark_return"] == round(4570.0 / 4600.0 - 1, 6)

    def test_does_not_overwrite_existing_value(self):
        """幂等保护: 已有真值不覆盖 (防止 18:05 重跑把 16:35 真值冲掉)"""
        db = _pts_db(benchmark=0.123456)
        result = settle_benchmark_for_date(db, _DL(), "2026-09-10")
        row = db.conn.execute(
            "SELECT benchmark_return FROM paper_trading_stats WHERE date='2026-09-10'").fetchone()
        assert row["benchmark_return"] == 0.123456
        assert result["written"] is False


class TestSettleBenchmarkInsert:
    """兜底路径: 当日 PTS 行不存在 (16:35 锁堵 skipped 日) → INSERT 补行"""

    def test_inserts_row_when_missing(self):
        db = _pts_db(with_row=False)
        result = settle_benchmark_for_date(db, _DL(), "2026-09-10")
        assert result["written"] is True
        row = db.conn.execute(
            "SELECT total_signals, filled_signals, paper_return, benchmark_return "
            "FROM paper_trading_stats WHERE date='2026-09-10'").fetchone()
        # 信号面未知 → 诚实 NULL, 只填 benchmark 面
        assert row["total_signals"] is None
        assert row["filled_signals"] is None
        assert row["paper_return"] is None
        assert row["benchmark_return"] == round(4570.0 / 4600.0 - 1, 6)


class TestSettleBenchmarkDegradation:
    """诚实降级: 指数缺数/无前日 → 不写假值, 留痕不抛错"""

    def test_missing_current_index_row(self):
        rows = [{"date": "2026-09-08", "close": 4558.737},
                {"date": "2026-09-09", "close": 4600.0}]
        db = _pts_db()
        result = settle_benchmark_for_date(db, _DL(rows), "2026-09-10")
        assert result["written"] is False
        assert result["reason"] == "index_missing_current"
        row = db.conn.execute(
            "SELECT benchmark_return FROM paper_trading_stats WHERE date='2026-09-10'").fetchone()
        assert row["benchmark_return"] is None

    def test_no_data_layer(self):
        db = _pts_db()
        result = settle_benchmark_for_date(db, None, "2026-09-10")
        assert result["written"] is False
        assert result["reason"] == "no_data_layer"

    def test_data_layer_exception(self):
        class _Boom:
            def get_index_daily(self, *a, **k):
                raise RuntimeError("boom")
        db = _pts_db()
        result = settle_benchmark_for_date(db, _Boom(), "2026-09-10")
        assert result["written"] is False
        assert result["reason"] == "index_read_failed"
        row = db.conn.execute(
            "SELECT benchmark_return FROM paper_trading_stats WHERE date='2026-09-10'").fetchone()
        assert row["benchmark_return"] is None


class TestSettleBenchmarkBackfill:
    """回补腿: 历史行 benchmark NULL → 逐日补齐 (08-26~09-09 欠账一次清)"""

    def test_backfills_null_history(self):
        db = _pts_db(with_row=False)
        # 两天历史 NULL 行 + 当日行
        for d, closes in (("2026-09-09", None), ("2026-09-10", None)):
            db.conn.execute(
                "INSERT INTO paper_trading_stats "
                "(date, total_signals, filled_signals, paper_return, benchmark_return) "
                "VALUES (?, 5, 5, -0.003, NULL)", (d,))
        db.conn.commit()
        n = settle_benchmark_for_date(db, _DL(), "2026-09-10", backfill_days=2)
        r09 = db.conn.execute(
            "SELECT benchmark_return FROM paper_trading_stats WHERE date='2026-09-09'").fetchone()
        r10 = db.conn.execute(
            "SELECT benchmark_return FROM paper_trading_stats WHERE date='2026-09-10'").fetchone()
        assert r09["benchmark_return"] == round(4600.0 / 4558.737 - 1, 6)
        assert r10["benchmark_return"] == round(4570.0 / 4600.0 - 1, 6)


class TestHandlerWiring:
    """接线守卫: fx_settlement handler 必须调用 benchmark 结算腿"""

    def test_fx_settlement_calls_benchmark_leg(self):
        import inspect
        from ops_layer.orchestrator import PipelineOrchestrator
        src = inspect.getsource(PipelineOrchestrator._run_fx_settlement)
        assert "settle_benchmark" in src, "benchmark 结算腿未接入 fx_settlement"

    def test_pnl_tracker_reads_capital_from_config(self):
        """EX-1 修法②: initial_capital 硬编码 → cfg 读 (constraint_checker 同款)"""
        class _Cfg:
            def get(self, k, d=None):
                return {"execution.initial_capital": 500000.0}.get(k, d)
        from execution_layer.tracking.pnl_tracker import PnLTracker
        t = PnLTracker(db=None, stock_pool=None, config=_Cfg())
        assert t.initial_capital == 500000.0

    def test_pnl_tracker_defaults_without_config(self):
        """config 缺省回落 300000.0 (与 execution.yaml 现值一致, 旧夹具兼容)"""
        from execution_layer.tracking.pnl_tracker import PnLTracker
        t = PnLTracker(db=None, stock_pool=None)
        assert t.initial_capital == 300000.0

    def test_portfolio_curve_no_stale_benchmark_docstring(self):
        """EX-1 修法③: 残迹注释已清 — docstring 不再宣称「vs 基准（沪深300模拟）」"""
        import inspect
        from execution_layer.tracking.pnl_tracker import PnLTracker
        doc = PnLTracker.get_portfolio_value_curve.__doc__ or ""
        assert "沪深300模拟" not in doc, "基准半成品残迹注释未清"
