# -*- coding: utf-8 -*-
"""weekly_review.py — 周报 MVP 三行 (P0-6/7, 09-28)。

派单: 补本周三行 (推荐数/对错/净值)。素材实查 (09-28 04:0x):
本周推荐 23 条全在 09-24 (buy12/sell9/hold2, state 全=recommended 未了结);
本周 post_exit 0 笔 → 「对错」无已了结样本。

红线: 「说出来的薄是诚实」(09-26 会审定案) — 0 笔了结时对错显式打 None +
caveats 注明, 不凑 0 胜 0 负 (0/0 会假造成 0% 胜率)。
产出 dict 供 dashboard P4 报告页 + DM 卡同源渲染 (同一 writer 同一文本)。
"""
from __future__ import annotations


def build_weekly_review(conn, week_start: str, week_end: str) -> dict:
    """本周三行 + 诚实声明。

    Args:
        conn: sqlite3 连接 (row_factory 不限, 用 positional)。
        week_start/week_end: 'YYYY-MM-DD' 周界 (调用方算, 函数保持纯)。

    Returns:
        {rec_total, rec_by_verdict, rec_by_state,
         exits_n, exits_win, exits_loss, exits_avg_pct,
         nav_week_pct, nav_anchor_dates, caveats: list[str]}
    """
    caveats: list[str] = []

    # --- 行① 推荐数 (recommendation_tracking, 按 verdict/state 分组) ---
    rec_rows = conn.execute(
        "SELECT verdict, state FROM recommendation_tracking "
        "WHERE rec_date BETWEEN ? AND ?",
        (week_start, week_end),
    ).fetchall()
    rec_by_verdict: dict[str, int] = {}
    rec_by_state: dict[str, int] = {}
    for verdict, state in rec_rows:
        rec_by_verdict[verdict] = rec_by_verdict.get(verdict, 0) + 1
        rec_by_state[state] = rec_by_state.get(state, 0) + 1
    rec_total = len(rec_rows)
    if rec_total == 0:
        caveats.append(f"本周 ({week_start}~{week_end}) 无新推荐")

    # --- 行② 对错 (post_exit_tracking, entry_price>0 过滤脏数据) ---
    exit_rows = conn.execute(
        "SELECT total_return_pct FROM post_exit_tracking "
        "WHERE exit_date BETWEEN ? AND ? AND entry_price > 0",
        (week_start, week_end),
    ).fetchall()
    exits_n = len(exit_rows)
    exits_win = exits_loss = 0
    exits_avg_pct = None
    if exits_n:
        rets = [r[0] for r in exit_rows if r[0] is not None]
        exits_win = sum(1 for r in rets if r > 0)
        exits_loss = sum(1 for r in rets if r < 0)
        if rets:
            exits_avg_pct = round(sum(rets) / len(rets), 4)
    else:
        # 红线: 不凑 0 胜 0 负 — 显式声明
        caveats.append(
            "本周推荐均在持有期内, 尚无到达退出条件的标的 "
            "(对错一栏留空非系统故障)")

    # --- 行③ 净值 (paper_trading_stats 周界起止对比) ---
    nav_week_pct = None
    nav_anchor_dates: list[str] = []
    nav_rows = conn.execute(
        "SELECT date, paper_return FROM paper_trading_stats "
        "WHERE date BETWEEN ? AND ? ORDER BY date",
        (week_start, week_end),
    ).fetchall()
    if len(nav_rows) >= 2:
        first_date, first_ret = nav_rows[0]
        last_date, last_ret = nav_rows[-1]
        nav_anchor_dates = [first_date, last_date]
        if first_ret is not None and last_ret is not None:
            # paper_return 语义 = 累计收益率 (小数), 周净值 = 区间增量
            nav_week_pct = round((last_ret - first_ret) * 100, 2)
        if first_date != week_start:
            caveats.append(f"净值周首锚日偏移: 用 {first_date} 代 {week_start}")
    elif nav_rows:
        nav_anchor_dates = [nav_rows[0][0]]
        caveats.append("本周净值仅 1 个快照点, 无法算区间涨跌")
    else:
        caveats.append("paper_trading_stats 本周无快照 (纸面交易未启用或未跑)")

    return {
        "week_start": week_start, "week_end": week_end,
        "rec_total": rec_total,
        "rec_by_verdict": rec_by_verdict,
        "rec_by_state": rec_by_state,
        "exits_n": exits_n,
        "exits_win": exits_win,
        "exits_loss": exits_loss,
        "exits_avg_pct": exits_avg_pct,
        "nav_week_pct": nav_week_pct,
        "nav_anchor_dates": nav_anchor_dates,
        "caveats": caveats,
    }
