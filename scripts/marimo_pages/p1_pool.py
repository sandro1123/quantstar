"""P1 推荐池 — marimo 独立页 (09-29 02:00 窗, Day1 mock P1 落地)。

数据源: stock_pool (status!=exited) — 全部未退出推荐生命线。
与 p0 「行动卡」区分: p0 是「今日该执行什么」, p1 是「推荐池全貌」
(用户视角席 qlib 教训: 信号质量 vs 组合表现两个独立打分板分开)。
每行: code/name/verdict/confidence/added_reason (带 agent verdict 非排名话术,
P0-5 修法已并) /买入区间/止损/rr_ratio。只读 (铁律 #4)。
"""
import marimo

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
    return (ASOF,)


@app.cell
def _(C, ASOF):
    STALE = C.staleness_report(ASOF)
    return (STALE,)


@app.cell
def _(ASOF, C, STALE, mo):
    mo.md(C.watermark_html(ASOF, STALE))


@app.cell
def _(mo):
    mo.md("# P1 推荐池\n推荐池全貌（信号质量板）——与 p2 持仓明细（组合表现板）分开。")


@app.cell
def _(C):
    try:
        POOL = C.fetch_df("""
            SELECT sp.code AS 代码, IFNULL(si.name, sp.name) AS 名称,
                   sp.status, COALESCE(sp.verdict, '') AS 方向,
                   sp.initial_score AS 综合分, sp.confidence AS 置信,
                   sp.added_reason AS 推荐理由,
                   sp.entry_low AS 买入下限, sp.entry_high AS 买入上限,
                   sp.stop_loss AS 参考卖出线, sp.rr_ratio AS rr比,
                   sp.added_date AS 推荐日
            FROM stock_pool sp
            LEFT JOIN stock_info si ON si.code = sp.code
            WHERE sp.status != 'exited'
            ORDER BY sp.initial_score DESC""")
    except Exception:
        POOL = C.fetch_df("""
            SELECT code AS 代码, name AS 名称, status,
                   initial_score AS 综合分, added_date AS 推荐日,
                   entry_low AS 买入下限, entry_high AS 买入上限,
                   stop_loss AS 参考卖出线
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
        mo.md(f"### 🗂 推荐池全量（{len(POOL)} 只未退出，按综合分降序）"),
        mo.ui.table(POOL, pagination=True),
        mo.md("推荐理由带 agent verdict（P0-5 修法）；" +
              "「候选池第X位」排名话术已废（铁闸：永不回落）。"),
    ])


@app.cell
def _(C):
    try:
        TRACK = C.fetch_df("""
            SELECT code AS 代码, rec_date AS 推荐日, verdict AS 方向,
                   confidence AS 置信, confidence_tier AS 信心档, state AS 状态
            FROM recommendation_tracking
            WHERE state = 'tracking'
            ORDER BY confidence DESC""")
    except Exception:
        TRACK = None
    return (TRACK,)


@app.cell
def _(TRACK, mo):
    if TRACK is not None and len(TRACK):
        mo.vstack([
            mo.md(f"### 📡 在跟推荐（recommendation_tracking，{len(TRACK)} 只）"),
            mo.ui.table(TRACK, pagination=True),
        ])
    else:
        mo.md("### 📡 在跟推荐\n暂无 tracking 状态推荐。")


@app.cell
def _(ASOF, mo):
    mo.md(f"---\n数据截至 **{ASOF}**；缺口与陈旧项见各页水印（铁律 #12）。")


if __name__ == "__main__":
    app.run()
