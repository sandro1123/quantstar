"""R4-③ 17:05 写后校验哨兵测试 (统筹 sm-39c8b41402a748ee ①/#44 同窗件)。

语义三面:
  1. 断电回归 (critical): 当日零 zzshare + 有 industry_fallback →
     critical issue (08-28/31 断电案原态);
  2. 时序颠倒 (warning): 真概念已写 + fallback 混回 → warning issue
     (child 晚到 INSERT OR IGNORE, 次日自愈, 可观测即可);
  3. 干净日: 零 fallback → 无 issue; 旧库无表 → 无 issue 不炸。

哨兵今晚只 dry-run 只读 (不落真 issues 行), 排程生效归明日起 —
本文件只测收集器函数层 (纯只读 SELECT, 不碰 issues_ledger 写面)。
"""
import sqlite3

import pytest

from cli.issues import collect_concept_fallback_regression

SCHEMA = """
CREATE TABLE concept_daily (code TEXT, date TEXT, plate_code TEXT,
    plate_name TEXT, source TEXT, change_pct REAL);
CREATE TABLE pipeline_runs (run_id TEXT, pipeline_name TEXT, date TEXT,
    status TEXT, error_msg TEXT, created_at TEXT);
"""

D = "2026-09-01"


def _mk(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    conn.executescript(SCHEMA)
    return conn


def test_blackout_regression_critical(tmp_path):
    """断电案原态: 零真概念 + 5892 fallback → critical。"""
    conn = _mk(tmp_path)
    conn.executemany(
        "INSERT INTO concept_daily VALUES (?,?,?,?,?,?)",
        [("600001", D, "p1", "行业冒充", "industry_fallback", 0.0)] * 3)
    issues, asof = collect_concept_fallback_regression(conn, D)
    assert len(issues) == 1
    assert issues[0]["severity"] == "critical"
    assert "零真概念" in issues[0]["title_zh"]
    assert issues[0]["evidence"]["industry_fallback_rows"] == 3
    assert issues[0]["evidence"]["zzshare_rows"] == 0
    assert issues[0]["dedup_key"] == f"S12:concept_fallback:20260901"
    conn.close()


def test_time_inversion_mixed_warning(tmp_path):
    """时序颠倒: 真概念已写 + fallback 混回 → warning (次日自愈可观测)。"""
    conn = _mk(tmp_path)
    conn.executemany(
        "INSERT INTO concept_daily VALUES (?,?,?,?,?,?)",
        [("600001", D, "p1", "真概念", "zzshare_concept_15", 1.2)] * 5
        + [("600002", D, "p2", "行业冒充", "industry_fallback", 0.0)] * 2)
    issues, _ = collect_concept_fallback_regression(conn, D)
    assert len(issues) == 1
    assert issues[0]["severity"] == "warning"
    assert "时序颠倒" in issues[0]["title_zh"]
    assert issues[0]["evidence"]["zzshare_rows"] == 5
    assert issues[0]["evidence"]["industry_fallback_rows"] == 2
    conn.close()


def test_clean_day_no_issue(tmp_path):
    """干净日: 零 fallback (纯真概念) → 无 issue。"""
    conn = _mk(tmp_path)
    conn.executemany(
        "INSERT INTO concept_daily VALUES (?,?,?,?,?,?)",
        [("600001", D, "p1", "真概念", "zzshare_concept_15", 1.2)] * 5)
    issues, asof = collect_concept_fallback_regression(conn, D)
    assert issues == []
    assert asof is None
    conn.close()


def test_old_db_no_table_no_crash(tmp_path):
    """旧库无 concept_daily 表 → 无 issue 不炸 (不是回归; 采集异常另有外层降级)。"""
    conn = sqlite3.connect(str(tmp_path / "old.db"))
    conn.execute("CREATE TABLE other (x TEXT)")
    issues, asof = collect_concept_fallback_regression(conn, D)
    assert issues == []
    assert asof is None
    conn.close()


def test_registered_in_collectors_and_asof(tmp_path):
    """收集器挂 COLLECTORS 名册 + asof 取当日小管道最新行。"""
    from cli.issues import COLLECTORS
    names = [n for n, _ in COLLECTORS]
    assert "concept_daily_fallback_regression" in names
    conn = _mk(tmp_path)
    conn.executemany(
        "INSERT INTO concept_daily VALUES (?,?,?,?,?,?)",
        [("600002", D, "p2", "行业冒充", "industry_fallback", 0.0)] * 2)
    conn.executemany(
        "INSERT INTO pipeline_runs VALUES (?,?,?,?,?,?)", [
            ("r1", "concept_plates_daily", D, "success", None, "2026-09-01 16:40:00"),
            ("r2", "concept_plates_daily", "2026-08-31", "success", None, "2026-08-31 16:41:00"),
        ])
    issues, asof = collect_concept_fallback_regression(conn, D)
    assert len(issues) == 1
    assert str(asof).startswith("2026-09-01 16")
    conn.close()
