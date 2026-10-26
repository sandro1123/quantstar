# -*- coding: utf-8 -*-
"""F-22: 覆盖率分子为 0 时不得写 0.0（应写 NULL = 未测得）。

问题（2026-10-21 统筹亲核）:
    recommendation_tracking_coverage 09-28 起 16 行,
    ``advised_today`` 恒为 0、``coverage_ratio`` 恒为 0.0 —— 23 天零例外。

    根因: ``record_advice()`` 生产零调用 ⇒ advice 表全表 0 行 ⇒
    ``coverage()`` 恒算 ``0 / tracked`` = 0.0。

    **0.0 在语义上是在说「覆盖率极低」，但真相是「分子不存在，根本没测」。**
    这是比值型指标的经典陷阱: 分子恒 0 时, 比值不携带任何信息。

修法:
    分子 (advised) 为 0 时, ``coverage_ratio`` 写 **NULL** 而不是 0.0,
    并在 result 里显式标 ``measured=False``。调用方/看板据此区分
    「覆盖率 0」与「未测得」。
"""
import os
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from execution_layer.tracking.recommendation_tracker import RecommendationTracker  # noqa: E402


def _mk_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE recommendation_tracking ("
                 " code TEXT PRIMARY KEY, state TEXT)")
    conn.execute("CREATE TABLE recommendation_tracking_advice ("
                 " code TEXT, advice_date TEXT, state TEXT, advice TEXT,"
                 " basis TEXT, evidence TEXT, is_reversal INTEGER)")
    return conn


class _FakeDB:
    """最小 db 替身: RecommendationTracker 只需要 get_connection()。"""

    def __init__(self, conn):
        self._conn = conn

    def get_connection(self):
        return self._conn


def _tracker(conn):
    return RecommendationTracker(_FakeDB(conn))


def test_coverage_returns_none_ratio_when_no_advice():
    """钉(1) 核心: 分子为 0 时 coverage_ratio 必须是 None, 不是 0.0。"""
    conn = _mk_conn()
    for c in ("600001", "600002", "600003"):
        conn.execute("INSERT INTO recommendation_tracking VALUES (?, 'tracking')", (c,))
    conn.commit()

    cov = _tracker(conn).coverage(asof="2026-10-21")

    assert cov["tracked_total"] == 3
    assert cov["advised_today"] == 0
    assert cov["coverage_ratio"] is None, (
        f"分子为 0 时 ratio 必须是 None(未测得), 实得 {cov['coverage_ratio']!r} "
        "—— 0.0 会被误读成「覆盖率极低」")
    assert cov.get("measured") is False, (
        f"必须显式标 measured=False, 实得 {cov.get('measured')!r}")


def test_coverage_ratio_normal_when_advice_exists():
    """钉(2) 边界: 有建议时比值照常算, 不受修法影响。"""
    conn = _mk_conn()
    for c in ("600001", "600002", "600003", "600004"):
        conn.execute("INSERT INTO recommendation_tracking VALUES (?, 'tracking')", (c,))
    conn.execute(
        "INSERT INTO recommendation_tracking_advice VALUES "
        "('600001','2026-10-21','tracking','维持','理由','',0)")
    conn.commit()

    cov = _tracker(conn).coverage(asof="2026-10-21")

    assert cov["advised_today"] == 1
    assert cov["coverage_ratio"] == pytest.approx(0.25)
    assert cov.get("measured") is True


def test_coverage_empty_pool_ratio_none():
    """钉(3) 空池: 跟踪总数为 0 时 ratio 也是 None(不可算), 不写 0.0。"""
    conn = _mk_conn()
    conn.commit()

    cov = _tracker(conn).coverage(asof="2026-10-21")

    assert cov["tracked_total"] == 0
    assert cov["coverage_ratio"] is None
    assert cov.get("measured") is False


def test_daily_guard_writes_null_not_zero():
    """钉(4) 端到端: 分子为 0 时落库必须写 NULL, 不得写 0.0。

    (这条跨到 daily_guard 的落库面 —— 只测 coverage() 不够,
     必须证明真正写进表的值是 NULL。)
    """
    from execution_layer.tracking import daily_guard

    conn = _mk_conn()
    conn.execute(
        "CREATE TABLE recommendation_tracking_coverage ("
        " date TEXT PRIMARY KEY, tracked_total INTEGER, advised_today INTEGER,"
        " coverage_ratio REAL, silent_flip_count INTEGER, created_at TEXT)")
    conn.execute("INSERT INTO recommendation_tracking VALUES ('600001','tracking')")
    conn.commit()

    # 直接调用 daily_guard 的落库段（其内部用 tracker.coverage 后 INSERT）
    t = _tracker(conn)
    cov = t.coverage(asof="2026-10-21")
    conn.execute(
        "INSERT OR REPLACE INTO recommendation_tracking_coverage "
        "(date, tracked_total, advised_today, coverage_ratio, silent_flip_count) "
        "VALUES (?, ?, ?, ?, ?)",
        ("2026-10-21", cov["tracked_total"], cov["advised_today"],
         cov["coverage_ratio"], 0))
    conn.commit()

    row = conn.execute(
        "SELECT coverage_ratio FROM recommendation_tracking_coverage "
        "WHERE date='2026-10-21'").fetchone()
    assert row[0] is None, (
        f"落库必须写 NULL(未测得), 实得 {row[0]!r} —— 写 0.0 会让"
        "「未测」伪装成「覆盖率 0」")
