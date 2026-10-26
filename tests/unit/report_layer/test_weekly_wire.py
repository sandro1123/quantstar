# -*- coding: utf-8 -*-
"""WeeklyReviewSection 接线 TDD (09-29, 判例「建了没接线」第八变体修)。

weekly_review.py 纯函数建了没接 sections() — WeeklyReviewSection 把三行
(推荐数/对错/净值) 接进周报。诚实水位: 0 笔了结对错「—」+ caveats 不凑 0 胜 0 负。
"""
import sqlite3
from unittest.mock import MagicMock

import pytest


def _ctx(date="2026-09-26", exits_rows=None, rec_rows=None, nav_rows=None):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE recommendation_tracking "
                 "(code TEXT, rec_date TEXT, verdict TEXT, state TEXT)")
    conn.execute("CREATE TABLE post_exit_tracking "
                 "(exit_date TEXT, entry_price REAL, total_return_pct REAL)")
    conn.execute("CREATE TABLE paper_trading_stats (date TEXT, paper_return REAL)")
    for code, rd, v, s in (rec_rows or []):
        conn.execute("INSERT INTO recommendation_tracking VALUES (?, ?, ?, ?)",
                     (code, rd, v, s))
    for ed, ep, tr in (exits_rows or []):
        conn.execute("INSERT INTO post_exit_tracking VALUES (?, ?, ?)", (ed, ep, tr))
    for d, pr in (nav_rows or []):
        conn.execute("INSERT INTO paper_trading_stats VALUES (?, ?)", (d, pr))
    db = MagicMock()
    db.get_connection.return_value = conn
    dl = MagicMock()
    dl.db = db
    ctx = MagicMock()
    ctx.date = date
    ctx.data_layer = dl
    return ctx


def _build(ctx):
    from report_layer.reports.weekly_summary import WeeklyReviewSection
    return WeeklyReviewSection().build(ctx)


# 钉①: sections() 含 WeeklyReviewSection (接线实证)
def test_sections_contains_weekly_review():
    from report_layer.reports.weekly_summary import WeeklySummaryBuilder
    names = [type(s).__name__ for s in WeeklySummaryBuilder().sections()]
    assert "WeeklyReviewSection" in names, f"sections 须含 WeeklyReviewSection: {names}"


# 钉②: 本周推荐 23 条 (buy12/sell9/hold2) 三行透出
def test_rec_count_three_lines():
    # 模拟 2026-09-26 (周六推送)，对应自然周 2026-09-21 ~ 2026-09-27
    recs = [("6005%02d" % i, "2026-09-24", "buy" if i < 12 else ("sell" if i < 21 else "hold"),
             "recommended") for i in range(23)]
    ctx = _ctx(date="2026-09-26", rec_rows=recs)
    out = _build(ctx)
    assert out is not None and out.name == "weekly_review"
    assert "推荐 23 条" in out.body
    assert "买 12" in out.body and "卖 9" in out.body and "持 2" in out.body


# 钉③: 0 笔了结对错「—」不凑 0 胜 0 负 + 诚实水位
def test_zero_exits_honest_dash():
    ctx = _ctx(date="2026-09-26", rec_rows=[("600519", "2026-09-24", "buy", "recommended")])
    out = _build(ctx)
    assert "—" in out.body and "无了结" in out.body, "0 笔了结须显式打 — 不凑 0 胜 0 负"
    assert "0 胜 0 负" not in out.body


# 钉④: 有了结 + 净值 → 对错+净值透出
def test_exits_and_nav():
    ctx = _ctx(
        date="2026-09-26",
        rec_rows=[("600519", "2026-09-24", "buy", "recommended")],
        exits_rows=[("2026-09-25", 10.0, 5.0), ("2026-09-26", 10.0, -2.0)],
        nav_rows=[("2026-09-22", 0.010), ("2026-09-26", 0.025)],
    )
    out = _build(ctx)
    assert "1 胜 1 负" in out.body, f"对错须透出: {out.body}"
    assert "+1.50%" in out.body or "1.5" in out.body, f"净值须透出: {out.body}"
