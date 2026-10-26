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
    import altair as alt
    return C, alt, mo


@app.cell
def _(C):
    ASOF = C.data_asof()
    STALE = C.staleness_report(ASOF)
    return ASOF, STALE


@app.cell
def _(ASOF, C, STALE, mo):
    mo.md(C.watermark_html(ASOF, STALE))


@app.cell
def _(mo):
    mo.md("# P2 因子健康")


@app.cell
def _(ASOF, C, mo):
    IC_DATE = C.one("SELECT MAX(calc_date) FROM factor_ic")[0]
    # F席 09-16: 期望日走 _common 单点真源 (factor_ic 设计滞后 T−ic_horizon
    # 交易日)。旧实现拿 IC_DATE 与 ASOF 裸比 → 恒报假红 —— 即统筹「E6 空转」
    # 判断的来源。反面断言锚点: 本文件不得再出现该裸比表达式 (见
    # tests/unit/scripts/test_p2_staleness.py 末测)。
    _floor = C.expected_floor("factor_ic", ASOF)
    IC_STALE = bool(IC_DATE) and _floor is not None and str(IC_DATE) < str(_floor)
    return IC_DATE, IC_STALE


@app.cell
def _(ASOF, C, IC_DATE, IC_STALE, mo):
    (mo.md(f"⚠️ **factor_ic 停在 {IC_DATE}（超出设计滞后）**——ic_feedback 链待查，"
           "本页因子结论基于旧数据，谨慎引用。") if IC_STALE else
     mo.md(f"factor_ic 最新计算日 {IC_DATE}（设计滞后 T−{C.expected_lag_days('factor_ic')} 交易日），在位。"))


@app.cell
def _(mo):
    # 07-19 历史回填批口径说明 (统筹 0270ea38 裁定: 采【乙】仅页注, 不加 schema 列;
    # 理由: ALTER TABLE 是不可逆生产 schema 变更, 而两口径并存是**可读性问题**,
    # 页注可解 —— 今夜已开"两套修法并存"技术债, 不宜再加 schema 债)。
    # 口径实测依据 (F席 09-16, 5 因子 × 5790 样本逐期比对, 容差 5e-4):
    #   legacy = 滚动 20 期·不含当日 (std 99.7% / icir 98.5% / win_rate 99.7% 吻合);
    #   含当日仅 32.1% / 20.4% / 57.3% 吻合 → legacy 非累计, 亦非含当日。
    mo.md(
        "ⓘ **口径说明**：本表 IC 统计为【滚动 20 期】口径。\n"
        "2026-07-13 及之前由历史回填批写入，07-14~08-11 无数据，08-12 起恢复。\n"
        "两段同族（均滚动 20 期），但【边界期形态不同】：\n"
        "legacy 在 ramp 期写值（用 ramp 窗），08-12 起新版 ramp 期写 NULL（宁缺勿脏）。\n"
        "故 ramp 期（各段前若干期）不可直接比较；满窗之后的稳态段可比。\n"
        "若将来需按口径/段筛选，须升为 `factor_ic.caliber` 列。")


@app.cell
def _(C, IC_DATE, mo):
    _n = C.window_days(10) if C.report_period() != "daily" else 10
    TOP = C.fetch_df("""
        SELECT factor_name, mean_ic, icir, win_rate, effectiveness
        FROM factor_ic WHERE calc_date = ?
        ORDER BY icir DESC LIMIT ?""", (IC_DATE, _n))
    BOT = C.fetch_df("""
        SELECT factor_name, mean_ic, icir, win_rate, effectiveness
        FROM factor_ic WHERE calc_date = ?
        ORDER BY icir ASC LIMIT ?""", (IC_DATE, max(5, _n // 2)))
    # ② 排序键退化告警 (F席 09-16, 统筹裁定判据): icir 无区分度时
    # ORDER BY icir DESC 与 ASC 同序 → BOT 退化为 TOP 的前缀 (实测 icir 全 NULL 时
    # COUNT(DISTINCT icir)=0, TOP10 == BOT10 逐位相同, BOT5 == TOP10[:5])。
    # 判据用"排序键无区分度"而非"集合相等": 后者**取决于取数条数**
    # (TOP10 vs BOT5 自然不等 → 恒 False → 告警永不触发 = 假绿)。
    _d = C.one("SELECT COUNT(DISTINCT icir) FROM factor_ic WHERE calc_date = ?", (IC_DATE,))
    DEGEN = bool(_d) and _d[0] is not None and _d[0] <= 1
    return BOT, DEGEN, TOP


@app.cell
def _(BOT, DEGEN, TOP, C, mo):
    _n = C.window_days(10) if C.report_period() != "daily" else 10
    _items = [
        mo.md(f"## ICIR Top{_n} / Bottom（最新计算日）"),
        mo.ui.table(TOP, pagination=False) if not TOP.empty else mo.md("factor_ic 空表。"),
        mo.md("**Bottom**"),
        mo.ui.table(BOT, pagination=False) if not BOT.empty else mo.md("无数据。"),
    ]
    if DEGEN:
        _items.insert(0, mo.md(
            "⚠️ **ICIR 全空/退化 —— 本页排序键失效，Top/Bottom 为同一批，结论不可用**"))
    mo.vstack(_items)


@app.cell
def _(C, IC_DATE, alt, mo):
    _win = C.window_days(30)  # #76：daily=30 / weekly=7 / monthly=30（周报口径「IC 变化」主窗）
    TREND = C.fetch_df("""
        WITH t5 AS (SELECT factor_name FROM factor_ic
                    WHERE calc_date = ? ORDER BY icir DESC LIMIT 5)
        SELECT calc_date, factor_name, mean_ic FROM factor_ic
        WHERE calc_date >= date(?, ?)
          AND factor_name IN (SELECT factor_name FROM t5)
        ORDER BY calc_date""", (IC_DATE, IC_DATE, f"-{_win} day"))
    return (TREND,)


@app.cell
def _(TREND, alt, mo):
    if TREND.empty:
        _view = mo.md("## IC 趋势\n窗口期内无 factor_ic 记录。")
    else:
        _chart = alt.Chart(TREND).mark_line(point=True).encode(
            x=alt.X("calc_date:O", title="计算日"),
            y=alt.Y("mean_ic:Q", title="mean IC"),
            color="factor_name:N",
            tooltip=["calc_date", "factor_name", "mean_ic"])
        _view = mo.vstack([
            mo.md(f"## ICIR Top5 因子近 {C.window_days(30)} 日 mean_ic 走势"),
            mo.ui.altair_chart(_chart),
        ])
    _view


@app.cell
def _(C, alt, mo):
    W_DATE = C.one("SELECT MAX(weight_date) FROM factor_weights")[0]
    WTS = C.fetch_df("""
        SELECT factor_name, weight, method FROM factor_weights
        WHERE weight_date = ? ORDER BY ABS(weight) DESC""", (W_DATE,))
    return WTS, W_DATE


@app.cell
def _(WTS, W_DATE, alt, mo):
    if WTS.empty:
        _view = mo.md("## 当前权重\nfactor_weights 空表。")
    else:
        _chart = alt.Chart(WTS).mark_bar().encode(
            x=alt.X("weight:Q", title="权重"),
            y=alt.Y("factor_name:N", sort="-x", title=None),
            tooltip=["factor_name", "weight", "method"])
        _view = mo.vstack([
            mo.md(f"## 当前因子权重（weight_date = {W_DATE}，method = {WTS['method'].iloc[0]}）"),
            mo.ui.altair_chart(_chart),
        ])
    _view


@app.cell
def _(C, IC_DATE, mo):
    EFF = C.fetch_df("""
        SELECT effectiveness, COUNT(*) AS n FROM factor_ic
        WHERE calc_date = ? GROUP BY effectiveness ORDER BY n DESC""", (IC_DATE,))
    return (EFF,)


@app.cell
def _(EFF, mo):
    mo.vstack([
        mo.md("## 有效性分档（最新计算日）"),
        mo.ui.table(EFF, pagination=False) if not EFF.empty else mo.md("无数据。"),
    ])


@app.cell
def _(C, mo):
    import sqlite3

    import shadow_chart

    # 影子模式段 (#101 切3): chart_analysis 三率对照 — shadow_display_html
    # 自护表缺 (返回占位文案); 此处只护连接层 (C.fetch_df 无 OperationalError
    # 防护, 页生态惯例 = 新 cell 自护), 连接失败不炸 export。
    try:
        _sc_conn = C.ro_conn()
        _shadow_view = mo.md(shadow_chart.shadow_display_html(_sc_conn))
        _sc_conn.close()
    except sqlite3.OperationalError:
        _shadow_view = mo.md(
            "## 影子模式 · LLM 读图对照\nchart_analysis 未迁移或数据库不可读 — "
            "表 v6 迁移 + 读图管道通电后此段自动出现。")
    _shadow_view


if __name__ == "__main__":
    app.run()
