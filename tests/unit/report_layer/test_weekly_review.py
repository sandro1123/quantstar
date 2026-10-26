# -*- coding: utf-8 -*-
"""weekly_review TDD (6 钉, P0-6/7 周报 MVP 09-28)。

红线: 「说出来的薄是诚实」— 0 笔了结时对错=None + caveats 显形,
不凑 0 胜 0 负 (0/0 假造成 0% 胜率)。夹具用真 DDL (判例)。
"""
import sqlite3

import pytest


def _conn():
    c = sqlite3.connect(":memory:")
    c.execute("""CREATE TABLE recommendation_tracking (
        id INTEGER PRIMARY KEY, code TEXT, rec_date TEXT, verdict TEXT,
        state TEXT, confidence REAL)""")
    c.execute("""CREATE TABLE post_exit_tracking (
        id INTEGER PRIMARY KEY, code TEXT, exit_date TEXT,
        entry_price REAL, total_return_pct REAL)""")
    c.execute("""CREATE TABLE paper_trading_stats (
        id INTEGER PRIMARY KEY, date TEXT, paper_return REAL)""")
    return c


def _build(c, ws="2026-09-22", we="2026-09-28"):
    from report_layer.sections.weekly_review import build_weekly_review
    return build_weekly_review(c, ws, we)


# W1: 存在性守卫 (判例#8)
def test_module_exists():
    try:
        import report_layer.sections.weekly_review  # noqa: F401
    except (ModuleNotFoundError, ImportError) as e:
        raise AssertionError(f"weekly_review 模块不存在: {e}")


# W2: 空周 (无推荐无了结) — 不抛错不除零 + caveats 显形
def test_empty_week():
    c = _conn()
    out = _build(c)
    assert out["rec_total"] == 0
    assert out["exits_n"] == 0
    assert out["exits_win"] is None or out["exits_win"] == 0
    assert out["caveats"], "空周必须有诚实声明"
    assert any("无新推荐" in x for x in out["caveats"])


# W3: 本周实数 — 23 条 (buy12/sell9/hold2)
def test_rec_count_by_verdict():
    c = _conn()
    recs = ([("buy", "recommended")] * 12 + [("sell", "recommended")] * 9
            + [("hold", "recommended")] * 2)
    for i, (v, s) in enumerate(recs):
        c.execute("INSERT INTO recommendation_tracking (code, rec_date, verdict, state) "
                  "VALUES (?, '2026-09-24', ?, ?)", (f"6000{i:02d}", v, s))
    out = _build(c)
    assert out["rec_total"] == 23
    assert out["rec_by_verdict"] == {"buy": 12, "sell": 9, "hold": 2}


# W4: 0 了结显形 — 对错=None 且 caveats 非空 (不是 0 胜 0 负)
def test_zero_exits_not_zero_win_loss():
    c = _conn()
    c.execute("INSERT INTO recommendation_tracking (code, rec_date, verdict, state) "
              "VALUES ('600519', '2026-09-24', 'buy', 'recommended')")
    out = _build(c)
    assert out["exits_n"] == 0
    assert out["exits_avg_pct"] is None, "0 笔了结不得假造平均"
    assert any("尚无到达退出条件" in x or "持有期" in x for x in out["caveats"]), \
        f"0 了结须显形: {out['caveats']}"


# W5: 净值锚偏移 — 缺周一 → 出数 + caveats 注明实际锚日
def test_nav_anchor_shift():
    c = _conn()
    # 周一 09-22 无快照, 首快照 09-23
    c.execute("INSERT INTO paper_trading_stats (date, paper_return) VALUES ('2026-09-23', 0.010)")
    c.execute("INSERT INTO paper_trading_stats (date, paper_return) VALUES ('2026-09-26', 0.025)")
    out = _build(c)
    assert out["nav_week_pct"] == pytest.approx(1.5, abs=0.01)
    assert out["nav_anchor_dates"] == ["2026-09-23", "2026-09-26"]
    assert any("锚日偏移" in x for x in out["caveats"])


# W6: entry_price=0 过滤 (14% 脏数据坑同族)
def test_entry_price_zero_filtered():
    c = _conn()
    # 1 笔 entry=0 脏数据 + 1 笔正常
    c.execute("INSERT INTO post_exit_tracking (code, exit_date, entry_price, total_return_pct) "
              "VALUES ('600519', '2026-09-25', 0, 99.9)")
    c.execute("INSERT INTO post_exit_tracking (code, exit_date, entry_price, total_return_pct) "
              "VALUES ('600520', '2026-09-25', 10.0, 5.0)")
    out = _build(c)
    assert out["exits_n"] == 1, "entry_price=0 脏数据须过滤"
    assert out["exits_win"] == 1
    assert out["exits_avg_pct"] == pytest.approx(5.0, abs=0.01)
