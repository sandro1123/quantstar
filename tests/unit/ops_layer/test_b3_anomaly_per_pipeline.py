# -*- coding: utf-8 -*-
"""B3: scan_recent_runs 按管道取 N 条 —— 测量仪器修复。

问题（2026-10-21 统筹实测）:
    anomaly_detector.scan_recent_runs 用全局 ``ORDER BY start_time DESC LIMIT 20``
    取最近 20 条 pipeline_runs, 再按管道聚合算连续失败 streak。

    但生产单日就有 130+ 条 pipeline_runs（10-20=134 / 10-19=131）。
    在 20 条窗口里, 一个每日只跑一次的管道最多出现 **1 次**,
    根本凑不满 CONSECUTIVE_THRESHOLD(3) ⇒ **全体日频管道的
    consecutive_failures 恒不可达** ⇒ 管道连续失败 50 天也不会告警。

    实证: #81 的 detail 冻结在 2026-09-01「连续失败3次」, 而实际已连续失败 22 次。

修法: 不再全局取 20 条, 而是**按管道分别取最近 N 条**再算 streak。

V1 例外注记（必须保留）:
    health_check 是 ``0 10,14 * * 1-5`` + ``6 16 * * 1-5`` 一日三检,
    是极少数能在「单日」内就形成连击的管道。它依然要能被正确检出。
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))), "src"))

from ops_layer.anomaly_detector import AnomalyDetector  # noqa: E402


class _FakeDB:
    """最小 db 替身: 只提供 get_connection()。"""

    def __init__(self, conn):
        self._conn = conn

    def get_connection(self):
        return self._conn


def _make_detector(conn):
    det = AnomalyDetector.__new__(AnomalyDetector)
    det.db = _FakeDB(conn)
    det._baselines = {}
    return det


def _mk_db(rows):
    """rows: [(pipeline_name, status, start_time, duration_s), ...]"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE pipeline_runs ("
        " pipeline_name TEXT, status TEXT, start_time TEXT, duration_s REAL)")
    conn.executemany(
        "INSERT INTO pipeline_runs VALUES (?,?,?,?)", rows)
    conn.commit()
    return conn


def _busy_day(conn, day):
    """往指定日期灌 130 条别的管道的记录, 模拟生产单日 130+ 行的噪声。

    这些记录时间都比目标管道"更新", 会把全局 LIMIT 20 的窗口占满。
    """
    rows = []
    for i in range(130):
        rows.append((f"noise_pipe_{i % 40}", "success",
                     f"{day} 23:5{ i % 10 }:00", 1.0))
    conn.executemany("INSERT INTO pipeline_runs VALUES (?,?,?,?)", rows)
    conn.commit()


def test_daily_pipeline_consecutive_failures_detected_under_busy_day():
    """钉(1) 正向: 单日 130+ 行噪声下, 日频管道连续 3 次失败必须被检出。

    这是原实现的**致命盲区**: 全局 LIMIT 20 里该管道只出现 1 次, streak=1 < 3。
    """
    conn = _mk_db([])
    # 目标管道: 连续 3 个交易日失败, 每天只有 1 条, 且都是当天 16:00
    for day in ("2026-10-15", "2026-10-16", "2026-10-19"):
        conn.execute("INSERT INTO pipeline_runs VALUES (?,?,?,?)",
                     ("daily_update", "failed", f"{day} 16:00:00", 12.0))
    # 噪声: 之后又有 130 条别的管道的成功记录(时间更新)
    _busy_day(conn, "2026-10-20")
    conn.commit()

    det = _make_detector(conn)
    out = det.scan_recent_runs()

    hits = [a for a in out
            if a.get("pipeline") == "daily_update"
            and a.get("anomaly_type") == "consecutive_failures"]
    assert hits, (
        "日频管道连续失败 3 次未被检出 —— 全局 LIMIT 窗口被单日噪声挤爆, "
        f"实得 anomalies={out}")
    assert hits[0]["severity"] == "critical", "连续失败应为 critical"


def test_multi_check_pipeline_streak_still_detected():
    """钉(2) V1 例外: 一日三检的 health_check 仍能正确算连击。"""
    conn = _mk_db([])
    for hh in ("10", "14", "16"):
        conn.execute("INSERT INTO pipeline_runs VALUES (?,?,?,?)",
                     ("health_check", "failed", f"2026-10-20 {hh}:00:00", 5.0))
    conn.commit()

    det = _make_detector(conn)
    out = det.scan_recent_runs()
    hits = [a for a in out if a.get("pipeline") == "health_check"]
    assert hits, "一日三检管道连续失败 3 次未被检出"
    assert hits[0]["anomaly_type"] == "consecutive_failures"


def test_streak_broken_by_success_not_flagged():
    """钉(3) 反向: 失败 2 次 + 成功 1 次(最新), 不得误报。"""
    conn = _mk_db([])
    conn.execute("INSERT INTO pipeline_runs VALUES (?,?,?,?)",
                 ("daily_update", "success", "2026-10-20 16:00:00", 10.0))
    conn.execute("INSERT INTO pipeline_runs VALUES (?,?,?,?)",
                 ("daily_update", "failed", "2026-10-19 16:00:00", 10.0))
    conn.execute("INSERT INTO pipeline_runs VALUES (?,?,?,?)",
                 ("daily_update", "failed", "2026-10-16 16:00:00", 10.0))
    _busy_day(conn, "2026-10-20")
    conn.commit()

    det = _make_detector(conn)
    out = det.scan_recent_runs()
    hits = [a for a in out if a.get("pipeline") == "daily_update"]
    assert not hits, f"最新已成功, 不应误报连续失败: {hits}"


def test_partial_counts_as_not_failed_but_streak_stops():
    """钉(4) 边界: partial 打断 streak（partial 不是 failed）。"""
    conn = _mk_db([])
    conn.execute("INSERT INTO pipeline_runs VALUES (?,?,?,?)",
                 ("daily_data_pipeline", "partial", "2026-10-20 15:30:00", 60.0))
    for day in ("2026-10-16", "2026-10-15", "2026-10-14"):
        conn.execute("INSERT INTO pipeline_runs VALUES (?,?,?,?)",
                     ("daily_data_pipeline", "failed", f"{day} 15:30:00", 60.0))
    _busy_day(conn, "2026-10-20")
    conn.commit()

    det = _make_detector(conn)
    out = det.scan_recent_runs()
    hits = [a for a in out if a.get("pipeline") == "daily_data_pipeline"]
    assert not hits, f"最新一条是 partial, streak 应被打断: {hits}"


def test_no_rows_returns_empty_not_crash():
    """钉(5) 空表: 返回空列表, 不抛异常。"""
    conn = _mk_db([])
    det = _make_detector(conn)
    assert det.scan_recent_runs() == []
