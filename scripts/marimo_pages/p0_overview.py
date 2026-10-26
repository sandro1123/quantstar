import marimo

__generated_with = "0.24.0"
app = marimo.App(width="full")


@app.cell
def _():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import _common as C
    import marimo as mo
    return C, mo


@app.cell
def _(C):
    ASOF = C.data_asof()
    STALE = C.staleness_report(ASOF)
    OPS = C.ops_oneliner()
    return ASOF, OPS, STALE


@app.cell
def _(ASOF, C, STALE, mo):
    mo.md(C.watermark_html(ASOF, STALE))


@app.cell
def _(C):
    # Day1 mock 主页大数字三枚 (09-29 01:00 窗): 30 日战绩事件口径非行口径。
    # 数据源与周报 MVP 同源 (weekly_review.py), 口径锁:
    #   胜率 = post_exit 近 30 日 win/(win+loss), entry_price>0 过滤 (14% 脏数据坑)
    #   均笔 = 同集 avg(total_return_pct) 百分数值不 ×100 (战绩页两坑之一)
    #   在跟 = recommendation_tracking state='tracking' distinct code
    try:
        import datetime as _dt
        _today = _dt.date.today().isoformat()
        _d30 = (_dt.date.today() - _dt.timedelta(days=30)).isoformat()
        _exits = C.fetch_df(
            "SELECT total_return_pct FROM post_exit_tracking "
            "WHERE exit_date BETWEEN ? AND ? AND entry_price > 0 "
            "AND total_return_pct IS NOT NULL", (_d30, _today))
        _track = C.fetch_df(
            "SELECT COUNT(DISTINCT code) n FROM recommendation_tracking "
            "WHERE state = 'tracking'")
        STATS = {"win_rate": None, "avg_pct": None, "tracking_n": int(_track["n"].iloc[0])}
        if len(_exits):
            _rets = _exits["total_return_pct"].astype(float)
            _wins = int((_rets > 0).sum())
            _losses = int((_rets < 0).sum())
            if _wins + _losses:
                STATS["win_rate"] = round(_wins / (_wins + _losses) * 100, 1)
            STATS["avg_pct"] = round(float(_rets.mean()), 2)
            STATS["exits_n"] = len(_rets)
        else:
            STATS["exits_n"] = 0
    except Exception:
        STATS = {"win_rate": None, "avg_pct": None, "tracking_n": 0, "exits_n": 0}
    return (STATS,)


@app.cell
def _(STATS, mo):
    _wr = f"{STATS['win_rate']}%" if STATS["win_rate"] is not None else "—"
    _ap = f"+{STATS['avg_pct']}%" if (STATS["avg_pct"] is not None and STATS["avg_pct"] >= 0) else (
        f"{STATS['avg_pct']}%" if STATS["avg_pct"] is not None else "—")
    mo.vstack([
        mo.md("### 📊 30 日战绩（事件口径）"),
        mo.ui.table([{
            "胜率": _wr,
            "平均单笔": _ap,
            "在跟推荐": STATS["tracking_n"],
            "30 日样本": STATS["exits_n"],
        }], pagination=False),
        mo.md("口径：推荐事件去重 · 近 30 日 · entry_price>0 过滤 · total_return_pct 为百分数值不 ×100。"),
    ])


@app.cell
def _(mo):
    mo.md("# P0 总览 + 行动卡\n决策前置：先看今天要做什么，再翻页深挖。")


@app.cell
def _(C):
    SIG = C.latest_signals()
    return (SIG,)


@app.cell
def _(SIG, mo):
    (mo.vstack([
        mo.md(f"### 📈 行动卡（信号日 {SIG['signal_date'].iloc[0]} ｜ pending_entry ｜ 按综合分排序）"),
        mo.ui.table(SIG, pagination=False),
        mo.md("买入区间/止损位为可执行数字；审批状态来自 human_review_queue 最新一条。"),
    ]) if not SIG.empty else
        mo.md("### 📈 行动卡\n近 14 日无 pending_entry 信号（stock_pool 空窗）。"))


@app.cell
def _(C):
    # TASK_20260922_01 Phase B: 全量候选表承接卡片折叠的「另有 N 只」 (5 分钟层)。
    # stock_info 展示层兜底名称 (81% 裸代码回填归数据层, 不属于本卡)。
    try:
        POOL = C.fetch_df("""
            SELECT sp.code AS 代码, IFNULL(si.name, sp.name) AS 名称,
                   sp.status, sp.initial_score AS 综合分,
                   sp.entry_low AS 买入下限, sp.entry_high AS 买入上限,
                   sp.stop_loss AS 参考卖出线, sp.take_profit AS 目标位,
                   sp.last_price AS 现价, sp.valid_until AS 有效期至
            FROM stock_pool sp
            LEFT JOIN stock_info si ON si.code = sp.code
            WHERE sp.status != 'exited'
            ORDER BY sp.initial_score DESC""")
    except Exception:
        POOL = C.fetch_df("""
            SELECT code AS 代码, name AS 名称, status,
                   initial_score AS 综合分, entry_low AS 买入下限,
                   entry_high AS 买入上限, stop_loss AS 参考卖出线,
                   take_profit AS 目标位, last_price AS 现价,
                   valid_until AS 有效期至
            FROM stock_pool WHERE status != 'exited'
            ORDER BY initial_score DESC""")
    _STATUS_CN = {"active": "持有中", "watch": "观察中",
                  "pending_entry": "待买入", "exit_pending": "待卖出"}
    POOL.insert(2, "状态", POOL["status"].map(
        lambda s: _STATUS_CN.get(str(s), str(s))))
    POOL = POOL.drop(columns=["status"])
    return (POOL,)


@app.cell
def _(POOL, mo):
    mo.vstack([
        mo.md("### 🗂 全量候选表（卡片「另有 N 只候选」的全量，按综合分排序）"),
        mo.ui.table(POOL, pagination=True),
    ])


@app.cell
def _(C):
    # TASK_20260922_01 Phase B: 完整故事线 (含已退出) 承接卡片聚合摘要。
    try:
        STORY = C.fetch_df("""
            SELECT IFNULL(si.name, sp.name) AS 名称, sp.code AS 代码, sp.status,
                   sp.added_date AS 推荐日, sp.entry_date AS 入场日,
                   sp.entry_price AS 入场价, sp.last_price AS 现价,
                   sp.exit_date AS 退出日, sp.exit_price AS 退出价,
                   COALESCE(sp.total_return_pct, sp.net_return_pct,
                            sp.unrealized_pnl_pct) AS 盈亏pct,
                   sp.valid_until AS 有效期至
            FROM stock_pool sp
            LEFT JOIN stock_info si ON si.code = sp.code
            ORDER BY (sp.status = 'exited') ASC, sp.added_date DESC""")
    except Exception:
        STORY = C.fetch_df("""
            SELECT name AS 名称, code AS 代码, status, added_date AS 推荐日,
                   entry_date AS 入场日, entry_price AS 入场价,
                   last_price AS 现价, exit_date AS 退出日,
                   exit_price AS 退出价,
                   COALESCE(total_return_pct, net_return_pct,
                            unrealized_pnl_pct) AS 盈亏pct,
                   valid_until AS 有效期至
            FROM stock_pool
            ORDER BY (status = 'exited') ASC, added_date DESC""")
    _STAGE_CN = {"active": "持有中", "watch": "观察中",
                 "pending_entry": "待买入", "exit_pending": "待卖出",
                 "exited": "已退出"}
    STORY.insert(2, "状态", STORY["status"].map(
        lambda s: _STAGE_CN.get(str(s), str(s))))
    STORY = STORY.drop(columns=["status"])
    return (STORY,)


@app.cell
def _(STORY, mo):
    mo.vstack([
        mo.md("### 🧵 完整故事线（全部推荐生命线，卡片只出聚合摘要）"),
        mo.ui.table(STORY, pagination=True),
    ])


@app.cell
def _(OPS, mo):
    mo.md(f"**🛠 运维一行**：{OPS}")


@app.cell
def _(ASOF, mo):
    mo.md(f"""
---
**深钻导航**（同日归档目录内）：P1 市场环境 ｜ P2 因子健康 ｜ P3 模型与 Agent 绩效 ｜
P4 回测与纸面交易（缺口占位）｜ P5 数据新鲜度与质量 ｜ P6 运维全景

数据截至 **{ASOF}**；缺口与陈旧项见各页水印（铁律 #12：不无声展示旧数）。
""")


if __name__ == "__main__":
    app.run()
