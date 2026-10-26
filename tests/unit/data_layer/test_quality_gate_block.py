"""Issue #12: 数据质量闸门阻断 (fail-closed) 单元测试

覆盖: DataQualityGate.check 各阶段阻断/放行; 依赖异常时保守阻断;
      SelectionPipeline.run 被质量闸门阻断后 blocked=True + 原因下放。
"""
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from common.database import Database
from data_layer.quality import DataQualityGate
from strategy_layer.aggregation.aggregator import FactorDaily
from strategy_layer.selection.selector import SelectionPipeline


# ---------------------------------------------------------------------------
# DataQualityGate.check 阶段测试
# ---------------------------------------------------------------------------

def _make_db_with_klines(date: str, n_rows: int, codes: list[str] | None = None,
                         add_history: bool = False) -> Database:
    """建内存库 + daily_klines 表，写入 n_rows 行 (date 固定)，可指定 code 集合。

    add_history=True 时为每个 code 补近 60 天历史行，供 TradingCalendar
    trading_days_between 计次新股上市天数 (≥60) 用；不影响当日行数统计。
    """
    db = Database(":memory:")
    conn = db.get_connection()
    conn.executescript("""
        CREATE TABLE daily_klines (
            code TEXT NOT NULL, date TEXT NOT NULL, open REAL, high REAL,
            low REAL, close REAL, volume REAL, amount REAL,
            pre_close REAL, change_pct REAL, PRIMARY KEY(code, date)
        );
        CREATE TABLE source_crosscheck_diffs (
            date TEXT NOT NULL, code TEXT NOT NULL, field TEXT NOT NULL,
            main_value REAL, ref_value REAL, diff REAL,
            created_at TEXT NOT NULL DEFAULT (datetime('now','+8 hours')),
            PRIMARY KEY(date, code, field)
        );
        CREATE TABLE data_quality_flags (
            code TEXT NOT NULL, date TEXT NOT NULL, table_name TEXT NOT NULL DEFAULT 'daily_klines',
            field TEXT NOT NULL, flag TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
            rule TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT (datetime('now','+8 hours')),
            PRIMARY KEY(code, date, table_name, field, rule)
        );
    """)
    codes = codes or [f"6{i:05d}" for i in range(n_rows)]
    for i, code in enumerate(codes[:n_rows]):
        conn.execute(
            "INSERT INTO daily_klines (code, date, open, high, low, close, volume, amount, pre_close, change_pct) "
            "VALUES (?, ?, 10.0, 11.0, 9.5, 10.5, 10000000, 105000000, 10.0, 5.0)",
            (code, date))
    if add_history and codes:
        from datetime import datetime as _dt
        base = _dt.strptime(date, "%Y-%m-%d") - timedelta(days=90)
        hist_rows = []
        for code in codes:
            for j in range(60):
                d = (base + timedelta(days=j)).isoformat()
                hist_rows.append((code, d, 10.0, 11.0, 9.5, 10.5, 10000000, 105000000, 10.0, 5.0))
        conn.executemany(
            "INSERT OR IGNORE INTO daily_klines (code, date, open, high, low, close, volume, amount, pre_close, change_pct) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", hist_rows)
    conn.commit()
    return db


def _make_candidates(date: str, codes: list[str]) -> list[FactorDaily]:
    return [FactorDaily(code=c, date=date, composite_score=0.5, factor_scores={}) for c in codes]


class TestQualityGate:
    def test_full_window_passes(self):
        """kline 行数充足 + 候选都有当日行 → 放行。"""
        db = _make_db_with_klines("2026-08-20", 5000,
                                  codes=[f"6{i:05d}" for i in range(5000)])
        gate = DataQualityGate(db, config={"min_daily_rows": 100})
        can, reason = gate.check("2026-08-20", codes=[f"6{i:05d}" for i in range(100)])
        assert can is True, f"应放行: {reason}"
        assert reason == ""

    def test_insufficient_daily_rows_blocks(self):
        """当日 daily_klines 行数 < min_daily_rows → 阻断。"""
        db = _make_db_with_klines("2026-08-20", 100)
        gate = DataQualityGate(db, config={"min_daily_rows": 4000})
        can, reason = gate.check("2026-08-20")
        assert can is False
        assert "4000" in reason or "行" in reason

    def test_candidate_missing_row_blocks(self):
        """候选池内某只股票当日无 kline 行 → 阻断。"""
        db = _make_db_with_klines("2026-08-20", 5000,
                                  codes=[f"6{i:05d}" for i in range(5000)])
        # 候选里加一只库内没有的股
        gate = DataQualityGate(db, config={"min_daily_rows": 100})
        can, reason = gate.check("2026-08-20",
                                 codes=[f"6{i:05d}" for i in range(100)] + ["999999"])
        assert can is False
        assert "缺行" in reason or "无行" in reason

    def test_crosscheck_diff_blocks(self):
        """source_crosscheck_diffs 差异行数超阈值 → 阻断。"""
        db = _make_db_with_klines("2026-08-20", 5000,
                                  codes=[f"6{i:05d}" for i in range(5000)])
        conn = db.get_connection()
        # 模拟多源对拍差异: 抽样 50 只, 差异阈值 5% → 3 只差异 (6%) 应阻断
        for i in range(3):
            conn.execute(
                "INSERT INTO source_crosscheck_diffs (date, code, field, main_value, ref_value, diff) "
                "VALUES ('2026-08-20', ?, 'close', 10.0, 11.0, 1.0)", (f"6{i:05d}",))
        conn.commit()
        gate = DataQualityGate(db, config={
            "min_daily_rows": 100,
            "crosscheck_sample": 50,
            "crosscheck_diff_rate": 0.05,
        })
        can, reason = gate.check("2026-08-20",
                                 codes=[f"6{i:05d}" for i in range(50)])
        assert can is False
        assert "交叉校验" in reason or "差异" in reason

    def test_crosscheck_diff_below_threshold_passes(self):
        """差异行数未达阈值比例 → 放行。"""
        db = _make_db_with_klines("2026-08-20", 5000,
                                  codes=[f"6{i:05d}" for i in range(5000)])
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO source_crosscheck_diffs (date, code, field, main_value, ref_value, diff) "
            "VALUES ('2026-08-20', '600001', 'close', 10.0, 11.0, 1.0)")
        conn.commit()
        gate = DataQualityGate(db, config={
            "min_daily_rows": 100,
            "crosscheck_sample": 50,
            "crosscheck_diff_rate": 0.05,
        })
        can, reason = gate.check("2026-08-20",
                                 codes=[f"6{i:05d}" for i in range(50)])
        assert can is True, f"应放行: {reason}"

    def test_flag_accumulation_blocks(self):
        """data_quality_flags 当日条数 >= max_daily_flags → 阻断。"""
        db = _make_db_with_klines("2026-08-20", 5000,
                                  codes=[f"6{i:05d}" for i in range(5000)])
        conn = db.get_connection()
        for i in range(500):
            conn.execute(
                "INSERT INTO data_quality_flags (code, date, field, flag, reason, rule) "
                "VALUES (?, '2026-08-20', 'pre_close', 'suspicious', 'x', 'pre_close_nonpositive')",
                (f"6{i:05d}",))
        conn.commit()
        gate = DataQualityGate(db, config={
            "min_daily_rows": 100,
            "max_daily_flags": 500,
        })
        can, reason = gate.check("2026-08-20")
        assert can is False
        assert "flag" in reason or "累积" in reason

    def test_flag_accumulation_below_threshold_passes(self):
        """flag 条数 < max_daily_flags → 放行。"""
        db = _make_db_with_klines("2026-08-20", 5000,
                                  codes=[f"6{i:05d}" for i in range(5000)])
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO data_quality_flags (code, date, field, flag, reason, rule) "
            "VALUES ('600001', '2026-08-20', 'pre_close', 'suspicious', 'x', 'pre_close_nonpositive')")
        conn.commit()
        gate = DataQualityGate(db, config={
            "min_daily_rows": 100,
            "max_daily_flags": 500,
        })
        can, reason = gate.check("2026-08-20")
        assert can is True, f"应放行: {reason}"

    def test_table_missing_blocks(self):
        """daily_klines 表不存在 → 保守阻断 (fail-closed)。"""
        db = Database(":memory:")
        conn = db.get_connection()
        conn.executescript("CREATE TABLE other (x INTEGER);")
        conn.commit()
        gate = DataQualityGate(db, config={"min_daily_rows": 100})
        can, reason = gate.check("2026-08-20")
        assert can is False
        assert "表" in reason or "缺失" in reason or "查询失败" in reason

    def test_disabled_gate_passthrough(self):
        """enabled=False → 始终放行，不查库。"""
        db = Database(":memory:")  # 无表
        gate = DataQualityGate(db, config={"enabled": False})
        can, reason = gate.check("2026-08-20")
        assert can is True
        assert reason == ""


# ---------------------------------------------------------------------------
# SelectionPipeline + 质量闸门集成测试
# ---------------------------------------------------------------------------

class TestSelectionWithQualityGate:
    def test_gate_blocks_selection(self):
        """质量闸门返回阻断 → SelectionPipeline.run blocked=True, candidates=[]。"""
        db = _make_db_with_klines("2026-08-20", 100)
        gate = DataQualityGate(db, config={"min_daily_rows": 4000})
        pipe = SelectionPipeline(data_layer=type("DL", (), {"db": db})(),
                                 quality_gate=gate)
        cands = _make_candidates("2026-08-20", ["600001", "600002"])
        result = pipe.run(cands)
        assert result.blocked is True
        assert result.candidates == []
        assert result.block_reason != ""
        assert "阻断" in result.block_reason or "kline" in result.block_reason

    def test_gate_allows_selection(self):
        """质量闸门放行 → SelectionPipeline 正常过滤返回候选。"""
        db = _make_db_with_klines("2026-08-20", 5000,
                                  codes=[f"6{i:05d}" for i in range(5000)] + ["600001", "600002"],
                                  add_history=True)
        gate = DataQualityGate(db, config={"min_daily_rows": 100})

        class _K:
            def __init__(self, code, date, close):
                self.code = code
                self.date = date
                self.close = close
                self.volume = 10000000   # 成交量 (股)
                self.amount = 105000000   # 成交额 (元), unit_price = 10.5 ≈ close, ≥ min_amount 5000 万

        class _DL:
            def __init__(self, db):
                self.db = db
                self._klines = {}
                conn = db.get_connection()
                for r in conn.execute("SELECT code, date, close FROM daily_klines WHERE date='2026-08-20'"):
                    self._klines.setdefault(r[0], []).append(_K(r[0], r[1], r[2]))

            def snapshot_range(self, start, end):
                class _Snap:
                    def __init__(self, ks):
                        self.klines = ks
                return [_Snap(self._klines.get(c, [])) for c in self._klines]

            def get_stock_info(self, code):
                class _Info:
                    list_date = "2020-01-01"
                return _Info()

        pipe = SelectionPipeline(data_layer=_DL(db), quality_gate=gate)
        cands = _make_candidates("2026-08-20", ["600001", "600002"])
        result = pipe.run(cands)
        assert result.blocked is False
        assert len(result.candidates) == 2, f"期望 2 只候选, 实际 {len(result.candidates)}: {result.filters_applied}"

    def test_no_quality_gate_unguarded(self):
        """未注入质量闸门时，SelectionPipeline 照常运行 (fail-open 兼容旧行为)。"""
        pipe = SelectionPipeline()
        cands = _make_candidates("2026-08-20", ["600001"])
        result = pipe.run(cands)
        assert result.blocked is False
        assert len(result.candidates) == 1

    def test_mutation_fail_closed_is_real(self):
        """变异验证 (AGENTS.md): 若 DataQualityGate.check 被篡回 fail-open
        (依赖异常时 continue 放行), 阻断断言必须报红。"""
        from data_layer.quality import DataQualityGate as DQG
        from tests.unit._mutation import assert_mutation_caught

        orig_check = DQG.check

        def _mutate_fail_open():
            def _fail_open(self, date, codes=None):
                # 旧 fail-open: 依赖异常/表缺失时直接放行 (不阻断)
                return True, ""
            DQG.check = _fail_open

        def _check_blocks_on_missing_table():
            db = Database(":memory:")  # 无 daily_klines 表
            gate = DQG(db, config={"min_daily_rows": 100})
            can, _ = gate.check("2026-08-20")
            assert can is False, "表缺失时必须阻断 (fail-closed)"

        try:
            assert_mutation_caught(_mutate_fail_open, _check_blocks_on_missing_table,
                                   "quality gate fail-closed (依赖失效须阻断)")
        finally:
            DQG.check = orig_check
