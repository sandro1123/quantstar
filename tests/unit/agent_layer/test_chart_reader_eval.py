# -*- coding: utf-8 -*-
"""#101 切3 对照评估段 RED (TDD) — 影子模式三指标 (设计稿 §5)

§5 先记录后对照 (影子→转正):
- 6周裁决窗三指标: 入场区触达率 / 止损先触率 / 止盈先触率 —
  atr_floor 基线组 vs llm_chart LLM修正组 双录对照;
- 裁决前 LLM 结论 = 影子模式 → 评估只读 chart_analysis + daily_klines,
  零写任何表 (含 signal/pending_entry, §1.5 唯一写者=02);
- 喂 F2 型稳定窗机器 = compare_sources 输出可直接消费的纯函数
  (F2 机器期2待施工, 本段先落记录形态)。

本卡裁定 (回执报统筹):
- 前视窗 = 图形日之后 EVAL_HORIZON_BARS 根日K (默认10, 两周);
- 同 bar 止损止盈双触 → 记止损先触 (保守口径);
- degraded 行 (llm_chart pattern IS NULL, 切2 判别键; atr_floor 行
  pattern 恒 NULL 故判别必须 source 门控) 不入 LLM 组 — 其数值即公式带,
  入组只会稀释对照 → 单独 degraded_rows 计数;
- 前视窗空 (未到期) → 不计 evaluated (宁缺毋滥, 不造 0%)。
"""
import pytest

from common.database import Database
from execution_layer.migrations import ensure_schema
from agent_layer.chart_reader.evaluation import EVAL_HORIZON_BARS, compare_sources

D = "2026-08-20"
D1, D2 = "2026-08-21", "2026-08-22"


def _mk_db():
    db = Database(":memory:")
    db.init_db()
    ensure_schema(db)
    conn = db.get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_klines (
            code TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL,
            volume REAL, amount REAL, pre_close REAL, change_pct REAL,
            turnover_rate REAL, adjust_type TEXT
        )""")
    conn.commit()
    return db


def _chart(conn, code, date, source, entry_low=10.0, entry_high=11.0,
           stop=8.5, profit=13.5, pattern="缩量回踩突破"):
    """播种 chart_analysis 行 (值受控; 表由真迁移器 v6 建, 不夹具自造列)。"""
    conn.execute(
        "INSERT INTO chart_analysis (code, date, pattern, entry_low, "
        "entry_high, stop, profit, source) VALUES (?,?,?,?,?,?,?,?)",
        (code, date, pattern, entry_low, entry_high, stop, profit, source))
    conn.commit()


def _kline(conn, code, date, high, low):
    conn.execute(
        "INSERT INTO daily_klines (code, date, open, high, low, close, volume) "
        "VALUES (?,?,?,?,?,?,?)",
        (code, date, low + 0.1, high, low, low + 0.2, 100))
    conn.commit()


# ================================================================ 组1: 三指标单行语义
class TestThreeMetrics:
    def test_entry_touch_then_profit_first(self):
        """入场区触达 + 止盈先触: 触带日再冲高 → 三率全对得上"""
        db = _mk_db()
        conn = db.get_connection()
        _chart(conn, "000001", D, "atr_floor")
        _kline(conn, "000001", D1, 11.8, 10.5)   # 触入场区, 未触损益
        _kline(conn, "000001", D2, 14.0, 11.0)   # 冲高触止盈
        r = compare_sources(conn)["atr_floor"]
        assert r["rows"] == 1 and r["evaluated"] == 1
        assert r["entry_touch"] == 1
        assert r["profit_first"] == 1 and r["stop_first"] == 0
        assert r["entry_touch_rate"] == 1.0
        assert r["profit_first_rate"] == 1.0 and r["stop_first_rate"] == 0.0
        db.close()

    def test_stop_first(self):
        """止损先触: bar 全程在入场带下方直接砸穿止损"""
        db = _mk_db()
        conn = db.get_connection()
        _chart(conn, "000001", D, "atr_floor")
        _kline(conn, "000001", D1, 9.5, 8.0)    # high 9.5 < entry_low → 未触带
        r = compare_sources(conn)["atr_floor"]
        assert r["entry_touch"] == 0
        assert r["stop_first"] == 1 and r["profit_first"] == 0
        assert r["stop_first_rate"] == 1.0
        db.close()

    def test_same_bar_double_touch_counts_stop(self):
        """同 bar 双触 (high 触止盈 low 触止损) → 保守口径记止损先触"""
        db = _mk_db()
        conn = db.get_connection()
        _chart(conn, "000001", D, "atr_floor")
        _kline(conn, "000001", D1, 14.0, 8.0)
        r = compare_sources(conn)["atr_floor"]
        assert r["stop_first"] == 1 and r["profit_first"] == 0
        db.close()

    def test_neither_touched_by_horizon(self):
        """前视窗内只触入场带, 损益均未触 → 三率只有触带率=1"""
        db = _mk_db()
        conn = db.get_connection()
        _chart(conn, "000001", D, "atr_floor")
        _kline(conn, "000001", D1, 10.9, 10.1)
        _kline(conn, "000001", D2, 10.9, 10.1)
        r = compare_sources(conn)["atr_floor"]
        assert r["entry_touch"] == 1
        assert r["stop_first"] == 0 and r["profit_first"] == 0
        db.close()

    def test_horizon_boundary_excludes_beyond(self):
        """前视窗=10根: 第11根才触损 → 不计 (窗口硬界)"""
        db = _mk_db()
        conn = db.get_connection()
        _chart(conn, "000001", D, "atr_floor")
        conn.executemany(
            "INSERT INTO daily_klines (code, date, open, high, low, close, volume) "
            "VALUES (?,?,?,?,?,?,?)",
            [("000001", "2026-08-%02d" % (20 + i), 10.3, 10.9, 10.1, 10.5, 100)
             for i in range(1, 11)]                 # D+1..D+10: 全程带内不触损益
            + [("000001", "2026-09-01", 10.3, 10.9, 8.0, 8.5, 100)])  # 第11根触损
        conn.commit()
        r = compare_sources(conn)["atr_floor"]
        assert r["evaluated"] == 1
        assert r["stop_first"] == 0    # 窗外不算
        db.close()

    def test_no_forward_window_pending(self):
        """前视窗空 (未到期) → 不计 evaluated, 率=None (不造0%)"""
        db = _mk_db()
        conn = db.get_connection()
        _chart(conn, "000001", D, "atr_floor")
        r = compare_sources(conn)["atr_floor"]
        assert r["evaluated"] == 0
        assert r["entry_touch_rate"] is None
        assert r["stop_first_rate"] is None and r["profit_first_rate"] is None
        db.close()


# ================================================================ 组2: 对照结构与影子纪律
class TestCompareSources:
    def test_degraded_excluded_from_llm_group(self):
        """degraded 行 (pattern NULL) 不入 LLM 组, 只入 degraded_rows 计数"""
        db = _mk_db()
        conn = db.get_connection()
        _chart(conn, "000001", D, "llm_chart")                        # 真结论
        _chart(conn, "000002", D, "llm_chart", pattern=None)         # degraded
        _kline(conn, "000001", D1, 14.0, 10.5)
        _kline(conn, "000002", D1, 14.0, 10.5)
        out = compare_sources(conn)
        assert out["llm_chart"]["rows"] == 1          # 只有真结论行
        assert out["llm_chart"]["evaluated"] == 1
        assert out["degraded_rows"] == 1
        db.close()

    def test_pure_read_zero_writes(self):
        """影子铁律: 评估全程零写 — total_changes 前后拍平 (fcdb 探针先例)"""
        db = _mk_db()
        conn = db.get_connection()
        _chart(conn, "000001", D, "atr_floor")
        _chart(conn, "000001", D, "llm_chart")
        _kline(conn, "000001", D1, 14.0, 10.5)
        conn.commit()
        before = conn.total_changes
        compare_sources(conn)
        assert conn.total_changes == before
        db.close()

    def test_date_filter_snapshot(self):
        """date=单日 → 只评估该日行 (累计=不传date, 喂F2形态)"""
        db = _mk_db()
        conn = db.get_connection()
        _chart(conn, "000001", D, "atr_floor")
        _chart(conn, "000001", "2026-08-19", "atr_floor")
        _kline(conn, "000001", "2026-08-21", 14.0, 10.5)
        out = compare_sources(conn, date=D)
        assert out["atr_floor"]["rows"] == 1
        assert out["atr_floor"]["evaluated"] == 1
        out_all = compare_sources(conn)
        assert out_all["atr_floor"]["rows"] == 2
        db.close()

    def test_empty_table_no_crash(self):
        """空表 → 全零结构不炸 (表建未通电期形态)"""
        db = _mk_db()
        out = compare_sources(db.get_connection())
        assert set(out) == {"atr_floor", "llm_chart", "degraded_rows"}
        assert out["atr_floor"]["rows"] == 0 and out["llm_chart"]["rows"] == 0
        assert out["degraded_rows"] == 0
        db.close()

    def test_horizon_constant_pinned(self):
        """前视窗=10根日K (两周) — 本卡裁定钉死, 变更须过统筹"""
        assert EVAL_HORIZON_BARS == 10
