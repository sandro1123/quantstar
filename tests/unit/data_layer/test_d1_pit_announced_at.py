"""D1 PIT 第一段: announced_at 保守回填 (announced_at+45 天口径) — TDD (红先行)。

架构定谳 (probe 09-11): 生产已有 announce_date 列 (DL-47, fuyao 真值,
10740/10803 已填) + _loader.py:968 PIT 读面过滤器。缺的两件:
1. **announced_at 保守回退列** — announce_date 为空的 63 行 (fuyao 12 季
   窗外/源端确无), PIT 读面现在「诚实降级为旧行为」(空行放行 = 前视
   偏差风险窗口)。本卡加 announced_at 列: 优先 announce_date 真值,
   空 则 报告期末+45 天保守上界 (fuyao 实测 lag 分布: 10-30 天 2176,
   30-45 天 561, 45-60 天 4341 — 45 天覆盖 ~34% 真实公告, 是「宁可晚见
   不早见」的保守口径, 与 D1 设计定谳一致)。
2. **PIT 读面切到 announced_at** — _loader 财务对齐过滤从
   announce_date 切 announced_at: 空值行为变化 = 空行不再放行 (旧行为),
   而是走 +45 天保守可见性 (新行可见性判定)。

幂等: announced_at 回填 = max(announce_date, period_end+45) 语义 —
重跑恒等; 45 天上界保证不早于任何可能真值 (保守 = 永不前视)。
"""
import sqlite3
from datetime import datetime, timedelta

import pytest

from data_layer.repos.financial import FinancialRepo
from scripts.backfill_announced_at import (
    compute_announced_at,
    run_backfill,
    CONSERVATIVE_LAG_DAYS,
)

SF_DDL = """
CREATE TABLE IF NOT EXISTS stock_financials (
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    eps REAL DEFAULT 0,
    roe REAL DEFAULT 0,
    revenue REAL DEFAULT 0,
    profit REAL DEFAULT 0,
    total_assets REAL DEFAULT 0,
    total_liabilities REAL DEFAULT 0,
    net_assets REAL DEFAULT 0,
    operating_cash_flow REAL DEFAULT 0,
    gross_margin REAL DEFAULT 0,
    net_margin REAL DEFAULT 0,
    announce_date TEXT DEFAULT '',
    caliber TEXT DEFAULT 'cumulative',
    PRIMARY KEY (code, date)
)
"""


class _Db:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row

    def get_connection(self):
        return self.conn


def _db(rows):
    db = _Db()
    db.conn.executescript(SF_DDL)
    for r in rows:
        db.conn.execute(
            "INSERT INTO stock_financials (code, date, eps, revenue, profit, announce_date) "
            "VALUES (?, ?, 1.0, 100.0, 50.0, ?)",
            (r["code"], r["date"], r.get("announce_date", "")))
    db.conn.commit()
    return db


class TestCompute:
    def test_conservative_lag_is_45(self):
        assert CONSERVATIVE_LAG_DAYS == 45

    def test_empty_announce_gets_period_end_plus_45(self):
        v = compute_announced_at("2026-06-30", "")
        assert v == "2026-08-14"  # 06-30 + 45d

    def test_real_announce_preferred_over_conservative(self):
        """真值存在时 announced_at = announce_date (真值优先, 不加 45)"""
        v = compute_announced_at("2026-06-30", "2026-07-15")
        assert v == "2026-07-15"

    def test_conservative_is_upper_bound_never_early(self):
        """保守口径永不早见: announced_at >= 真实可能公告日下界
        (报告期末+0 天最早可能) — 即 announced_at 恒 >= date。"""
        v = compute_announced_at("2026-06-30", "")
        assert v >= "2026-06-30"

    def test_boundary_exact_45_days(self):
        """边界: 03-31 期末 → announced_at = 05-15 (恰 45 天)"""
        v = compute_announced_at("2026-03-31", "")
        assert v == "2026-05-15"

    def test_invalid_date_returns_empty(self):
        assert compute_announced_at("garbage", "") == ""


class TestGuardAlter:
    def test_column_added_idempotent(self):
        db = _db([])
        repo_guard = run_backfill  # 直接跑回填即建列
        run_backfill(db.conn, dry_run=True)  # dry-run 也不建列 — 只读
        # dry-run 不改库: announced_at 列仍不存在
        cols = [r[1] for r in db.conn.execute(
            "PRAGMA table_info(stock_financials)").fetchall()]
        assert "announced_at" not in cols

    def test_apply_adds_column_and_backfills(self):
        db = _db([
            {"code": "600001", "date": "2026-06-30", "announce_date": "2026-07-15"},
            {"code": "600002", "date": "2026-06-30", "announce_date": ""},
        ])
        result = run_backfill(db.conn, dry_run=False)
        cols = [r[1] for r in db.conn.execute(
            "PRAGMA table_info(stock_financials)").fetchall()]
        assert "announced_at" in cols
        rows = db.conn.execute(
            "SELECT code, announced_at FROM stock_financials ORDER BY code").fetchall()
        assert rows[0]["announced_at"] == "2026-07-15"      # 真值
        assert rows[1]["announced_at"] == "2026-08-14"      # 保守 +45

    def test_apply_rerun_idempotent(self):
        """幂等: 已填行不重算不漂移"""
        db = _db([{"code": "600001", "date": "2026-06-30", "announce_date": ""}])
        run_backfill(db.conn, dry_run=False)
        run_backfill(db.conn, dry_run=False)  # 二跑
        row = db.conn.execute(
            "SELECT announced_at FROM stock_financials WHERE code='600001'").fetchone()
        assert row["announced_at"] == "2026-08-14"


class TestDryWriteParity:
    def test_dry_and_apply_identical_plan(self):
        """dry/write 恒等对拍: dry-run 的计划与 apply 的实际写逐行一致"""
        rows = [
            {"code": "600001", "date": "2026-06-30", "announce_date": "2026-07-15"},
            {"code": "600002", "date": "2026-03-31", "announce_date": ""},
            {"code": "600003", "date": "2025-09-30", "announce_date": ""},
        ]
        db_dry = _db(rows)
        db_write = _db(rows)
        dry = run_backfill(db_dry.conn, dry_run=True)
        wet = run_backfill(db_write.conn, dry_run=False)
        # 计划面恒等
        assert dry["n_update"] == wet["n_update"] == 3
        assert dry["from_real"] == wet["from_real"] == 1
        assert dry["from_conservative"] == wet["from_conservative"] == 2
        assert dry["plan"] == wet["plan"]


class TestPitReadFace:
    def test_loader_filters_on_announced_at(self):
        """PIT 读面切换守卫: _loader 财务对齐过滤键 = announced_at"""
        import inspect
        from data_layer import _loader
        src = inspect.getsource(_loader.DataLoader._get_financials_range)
        assert "announced_at" in src, "PIT 读面未切到 announced_at"

    def test_visibility_conservative_never_lookahead(self):
        """端到端: 空真值行走保守上界 — as_of < period+45 时不可见"""
        from data_layer._loader import DataLoader
        db = _db([
            {"code": "600001", "date": "2026-06-30", "announce_date": ""},  # 保守 08-14
            {"code": "600002", "date": "2026-06-30", "announce_date": "2026-07-10"},
        ])
        run_backfill(db.conn, dry_run=False)
        loader = DataLoader.__new__(DataLoader)  # 绕过 __init__ (只测 _get_financials_range)
        # as_of = 07-20: 600001 保守上界 08-14 未到 → 不可见; 600002 真值 07-10 已过 → 可见
        got = loader._get_financials_range(db.conn, "600001", "2020-01-01", "2026-07-20")
        assert got == [], "保守上界前行应不可见 (旧旧行为=放行, 新口径=不可见)"
        got2 = loader._get_financials_range(db.conn, "600002", "2020-01-01", "2026-07-20")
        assert len(got2) == 1
        # as_of = 08-15: 600001 上界已过 → 可见
        got3 = loader._get_financials_range(db.conn, "600001", "2020-01-01", "2026-08-15")
        assert len(got3) == 1
