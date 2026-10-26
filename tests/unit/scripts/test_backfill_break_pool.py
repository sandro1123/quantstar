# -*- coding: utf-8 -*-
"""TDD: backfill_break_pool 候选逻辑 (刀5, fix/break-pool-backfill)

只测**不碰生产库/外网**的部分: 候选日期选择逻辑 (库里 break_pool 为 0
且 daily_klines 当日有行) + 归一化字段口径。源拉取/写库路径靠 dry-run
默认 + --apply 显式授权链保障, 不在单测里打真源。
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

import backfill_break_pool as bfp  # noqa: E402


def _mem_db():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE daily_klines (code TEXT, date TEXT, close REAL,
                                   PRIMARY KEY(code, date));
        CREATE TABLE limit_break_pool (date TEXT, code TEXT, name TEXT,
            last_price REAL, change_pct REAL, open_times INTEGER,
            turnover_ratio REAL, turnover REAL, source TEXT,
            created_at TIMESTAMP, PRIMARY KEY(date, code));
    """)
    return conn


class TestCandidateSelection:
    def test_zero_rows_with_kline_is_candidate(self):
        """break_pool 0 行且 kline 有行 → 候选 (09-30/10-08 实证形态)。"""
        conn = _mem_db()
        for d in ("2026-09-28", "2026-09-29", "2026-10-09"):
            for i in range(3):  # 夹具象征性几行
                conn.execute(
                    "INSERT INTO daily_klines VALUES (?, ?, 1.0)",
                    (f"c{i}", d))
        conn.execute(
            "INSERT INTO limit_break_pool (date, code) VALUES ('2026-09-28', 'a')")
        conn.commit()
        dates, notes = bfp._candidate_dates(conn, "2026-09-25", "2026-10-10")
        assert "2026-09-29" in dates, f"0 行+kline 有行必须候选; 实得 {dates}"
        assert "2026-09-28" not in dates, "已有行不得候选"

    def test_present_days_never_candidates(self):
        """统筹三条之①: 库里已有数据的日期绝不入候选 (不重拉覆盖)。"""
        conn = _mem_db()
        for d in ("2026-09-28", "2026-09-29", "2026-10-09", "2026-10-12"):
            conn.execute("INSERT INTO daily_klines VALUES ('c0', ?, 1.0)", (d,))
        for d in ("2026-09-28", "2026-09-29", "2026-10-09", "2026-10-12"):
            conn.execute(
                "INSERT INTO limit_break_pool (date, code) VALUES (?, 'a')", (d,))
        conn.commit()
        dates, _ = bfp._candidate_dates(conn, "2026-09-25", "2026-10-15")
        assert dates == [], f"全 present 窗必须零候选; 实得 {dates}"

    def test_no_kline_day_not_candidate(self):
        """kline 当日 0 行 (真假日) → 不候选 (源无数据可拉)。"""
        conn = _mem_db()
        conn.execute("INSERT INTO daily_klines VALUES ('c0', '2026-09-28', 1.0)")
        conn.commit()
        dates, _ = bfp._candidate_dates(conn, "2026-09-25", "2026-10-10")
        assert "2026-09-29" not in dates, (
            f"kline 0 行日不得候选; 实得 {dates}")

    def test_calendar_divergence_noted_not_silent(self):
        """kline 有行但日历判非交易日 (10-08 实例) → 纳入候选+标注。"""
        conn = _mem_db()
        conn.execute("INSERT INTO daily_klines VALUES ('c0', '2026-10-08', 1.0)")
        conn.commit()
        dates, notes = bfp._candidate_dates(conn, "2026-10-01", "2026-10-10")
        assert "2026-10-08" in dates, "kline 事实优先, 不得因日历漏候选"
        assert "2026-10-08" in notes and "divergence" in notes["2026-10-08"], (
            f"日历分歧必须标注 (不静默); 实得 notes={notes}")

    def test_no_hardcoded_single_date(self):
        """候选逻辑不得硬编码 09-30 — 下一缺口自动覆盖 (统筹要求)。"""
        src = (Path(__file__).resolve().parents[3] / "scripts"
               / "backfill_break_pool.py").read_text(encoding="utf-8")
        assert '"2026-09-30"' not in src and "'2026-09-30'" not in src, (
            "候选逻辑不得硬编码具体日期 (下一缺口须自动覆盖)")

    def test_default_start_is_relative_not_hardcoded(self):
        """--from 默认=库内最早日期前 5 天 (参数化, 不写死)。"""
        conn = _mem_db()
        conn.execute(
            "INSERT INTO limit_break_pool (date, code) VALUES ('2026-01-10', 'a')")
        conn.commit()
        assert bfp._default_start(conn) == "2026-01-05"
        conn2 = _mem_db()
        assert bfp._default_start(conn2) == "2026-01-01"  # 空库兜底


class TestRowNormalization:
    def test_row_fields_match_collector_contract(self):
        """归一化字段口径与 shortterm_collect._collect_limit_break 一致。"""
        row = {"ticker": "600519", "thscode": "600519.SH", "name": "贵州茅台",
               "last_price": "1500.5", "price_change_ratio_pct": "3.2",
               "open_times": "2", "turnover_ratio_pct": "1.5",
               "turnover": "1000000"}
        code = bfp._code_from_row(row)
        assert code == "600519"
        assert bfp._safe_float("1500.5") == 1500.5
        assert bfp._safe_float(None) is None
        assert int(row["open_times"] or 0) == 2

    def test_code_from_thscode_when_ticker_missing(self):
        """无 ticker 时从 thscode 派生 (与 _collect_limit_break 同逻辑)。"""
        assert bfp._code_from_row({"thscode": "000001.SZ"}) == "000001"
        assert bfp._code_from_row({"thscode": ""}) == ""

    def test_insert_sql_is_or_replace(self):
        """写库必须幂等 (INSERT OR REPLACE, PK=(date,code))。"""
        assert "INSERT OR REPLACE" in bfp.INSERT_SQL
        assert "limit_break_pool" in bfp.INSERT_SQL
