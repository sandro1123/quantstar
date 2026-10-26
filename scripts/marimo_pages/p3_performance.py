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
    mo.md("# P3 模型与 Agent 绩效\n"
          "model_predictions 自带 actual_return_5d/10d/20d 列（无独立 fwd 表），"
          "命中率 = 每日 blended_score Top10 中实际收益 >0 的占比；未到窗口期的样本不计分母。")


@app.cell
def _(ASOF, C, mo):
    _win = C.window_days(30)  # #76：daily=30 / weekly≥7 / monthly≥30
    HIT = C.fetch_df("""
        WITH ranked AS (
            SELECT date, blended_score, actual_return_5d, actual_return_10d,
                   actual_return_20d,
                   ROW_NUMBER() OVER (PARTITION BY date
                                      ORDER BY blended_score DESC) AS rn
            FROM model_predictions
            WHERE date >= date(?, ?))
        SELECT date, COUNT(*) AS n,
               SUM(actual_return_5d  IS NOT NULL) AS n5,
               SUM(actual_return_5d  > 0) AS h5,
               AVG(actual_return_5d)  AS r5,
               SUM(actual_return_10d IS NOT NULL) AS n10,
               SUM(actual_return_10d > 0) AS h10,
               SUM(actual_return_20d IS NOT NULL) AS n20,
               SUM(actual_return_20d > 0) AS h20
        FROM ranked WHERE rn <= 10
        GROUP BY date ORDER BY date""", (ASOF, f"-{_win} day"))
    return (HIT,)


@app.cell
def _(HIT, alt, mo):
    if HIT.empty:
        _view = mo.md("## 模型命中率\n近 30 日无 model_predictions 记录。")
    else:
        _df = HIT.copy()
        for _h, _n in (("h5", "n5"), ("h10", "n10"), ("h20", "n20")):
            _df[f"hit_{_h[1:]}"] = (_df[_h] / _df[_n].replace(0, float("nan"))).round(3)
        _chart = alt.Chart(_df).mark_line(point=True).encode(
            x=alt.X("date:O", title="信号日"),
            y=alt.Y("hit_5:Q", title="Top10 命中率（5日）", scale=alt.Scale(domain=[0, 1])),
            tooltip=["date", "hit_5", "n5", "r5"])
        _view = mo.vstack([
            mo.md("## 每日 Top10 命中率（5 日窗口）"),
            mo.ui.altair_chart(_chart),
            mo.md("**分窗口明细**（n=样本数，nX/hX=成熟样本/命中数，r5=平均 5 日收益）"),
            mo.ui.table(_df, pagination=False),
        ])
    _view


@app.cell
def _(C, mo):
    AG = C.fetch_df("""
        SELECT agent_name, COUNT(*) AS n,
               SUM(actual_return IS NOT NULL) AS settled,
               SUM(CASE WHEN hit = 1 THEN 1 ELSE 0 END) AS hits,
               ROUND(AVG(actual_return), 4) AS avg_return
        FROM agent_predictions GROUP BY agent_name ORDER BY n DESC""")
    return (AG,)


@app.cell
def _(AG, mo):
    if AG.empty:
        _view = mo.md("## Agent 绩效\nagent_predictions 空表。")
    else:
        _df = AG.copy()
        _df["hit_rate"] = (_df["hits"] / _df["settled"].replace(0, float("nan"))).round(3)
        _view = mo.vstack([
            mo.md("## Agent 绩效（verdict 已按实际收益回填 hit）"),
            mo.ui.table(_df, pagination=False),
        ])
    _view


@app.cell
def _(C):
    # P0-1 推荐回溯表（recommendation_retrospect）：每条推荐的可回溯档案。
    # 表未建（DDL 待 GO）时本节降级为占位，不炸每日构建。
    RETRO_EXISTS = bool(C.one(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='recommendation_retrospect'"))
    return (RETRO_EXISTS,)


@app.cell
def _(C, RETRO_EXISTS):
    if not RETRO_EXISTS:
        RETRO_SUM = RETRO_VA = RETRO_TIER = RETRO_DETAIL = None
    else:
        RETRO_SUM = C.fetch_df("""
            SELECT COUNT(*) AS total,
                   COALESCE(SUM(hit_5d IS NOT NULL), 0)  AS n5,
                   COALESCE(SUM(hit_5d = 1), 0)          AS h5,
                   COALESCE(SUM(hit_10d IS NOT NULL), 0) AS n10,
                   COALESCE(SUM(hit_10d = 1), 0)         AS h10,
                   COALESCE(SUM(hit_5d IS NULL AND hit_10d IS NULL), 0) AS unsettled
            FROM recommendation_retrospect""")
        RETRO_VA = C.fetch_df("""
            SELECT verdict, agent_name, COUNT(*) AS n,
                   COALESCE(SUM(hit_5d IS NOT NULL), 0)  AS n5,
                   COALESCE(SUM(hit_5d = 1), 0)          AS h5,
                   COALESCE(SUM(hit_10d IS NOT NULL), 0) AS n10,
                   COALESCE(SUM(hit_10d = 1), 0)         AS h10
            FROM recommendation_retrospect
            GROUP BY verdict, agent_name
            ORDER BY verdict, agent_name""")
        RETRO_TIER = C.fetch_df("""
            SELECT CASE WHEN confidence >= 0.6 THEN 'high'
                        WHEN confidence >= 0.4 THEN 'mid'
                        ELSE 'low' END AS tier,
                   COUNT(*) AS n,
                   COALESCE(SUM(hit_5d IS NOT NULL), 0) AS n5,
                   COALESCE(SUM(hit_5d = 1), 0)         AS h5
            FROM recommendation_retrospect
            GROUP BY tier ORDER BY tier""")
        RETRO_DETAIL = C.fetch_df("""
            SELECT rec_date, code, agent_name, verdict, confidence,
                   blended_score, fwd_return_5d, hit_5d,
                   fwd_return_10d, hit_10d
            FROM recommendation_retrospect
            ORDER BY rec_date DESC, prediction_id DESC LIMIT 100""")
    return RETRO_DETAIL, RETRO_SUM, RETRO_TIER, RETRO_VA


@app.cell
def _(RETRO_DETAIL, RETRO_EXISTS, RETRO_SUM, RETRO_TIER, RETRO_VA, mo):
    import pandas as pd

    if not RETRO_EXISTS:
        _retro = mo.md(
            "## 推荐质量（推荐回溯表）\n"
            "⏳ `recommendation_retrospect` 未建——P0-1 DDL 待 GO，"
            "回填脚本已 dry-run 就绪（scripts/backfill_recommendation_retrospect.py）。")
    elif RETRO_SUM is None or not int(RETRO_SUM["total"].iloc[0]):
        _retro = mo.md("## 推荐质量（推荐回溯表）\n`recommendation_retrospect` 空表（未回填）。")
    else:
        _r = RETRO_SUM.iloc[0]
        _total = int(_r["total"])
        _hr5 = f"{int(_r['h5']) / int(_r['n5']):.1%}" if int(_r["n5"]) else "—"
        _hr10 = f"{int(_r['h10']) / int(_r['n10']):.1%}" if int(_r["n10"]) else "—"
        _cards = (
            '<div style="display:flex;gap:12px;flex-wrap:wrap;margin:8px 0;">'
            + "".join(
                f'<div style="border:1px solid #d0d7de;border-radius:6px;padding:8px 14px;'
                f'background:#f6f8fa;min-width:110px;">'
                f'<div style="font-size:12px;color:#57606a;">{label}</div>'
                f'<div style="font-size:20px;font-weight:600;">{val}</div></div>'
                for label, val in [
                    ("建档推荐", _total),
                    ("5日已结算", f"{int(_r['n5'])}"),
                    ("5日命中率", _hr5),
                    ("10日已结算", f"{int(_r['n10'])}"),
                    ("10日命中率", _hr10),
                    ("未结算", f"{int(_r['unsettled'])}"),
                ])
            + "</div>")
        _va = RETRO_VA.copy()
        _va["hit_rate_5d"] = (_va["h5"] / _va["n5"].replace(0, float("nan"))).round(3)
        _va["hit_rate_10d"] = (_va["h10"] / _va["n10"].replace(0, float("nan"))).round(3)
        _tier = RETRO_TIER.copy()
        _tier["hit_rate_5d"] = (_tier["h5"] / _tier["n5"].replace(0, float("nan"))).round(3)
        _det = RETRO_DETAIL.copy()
        for _c in ("confidence", "blended_score", "fwd_return_5d", "fwd_return_10d"):
            # 全 NULL 列 pandas 读成 object(None) dtype, .round() 逐元素调 round()
            # 抛 TypeError (NoneType 无 __round__)。实证: 08-27 首次真数据回填后,
            # 近 100 条明细 rec_date 全在 10 日窗口内 → fwd_return_10d 全 NULL →
            # 导出炸 cell。to_numeric(errors=coerce) 转 NaN 后 round 安全。
            _det[_c] = pd.to_numeric(_det[_c], errors="coerce").round(4)
        _retro = mo.vstack([
            mo.md("## 推荐质量（推荐回溯表 recommendation_retrospect）\n"
                  "口径 = AG-01 统一口径：推荐+收益>0 / 不推荐+收益<0 / "
                  "观望·警惕+|收益|<2% 记命中；收益为小数（factor_daily fwd_return ÷100）。"
                  "trace（当时因子快照+模型分+推理）存 trace_json，不含未来列。"),
            mo.md(_cards),
            mo.md("### verdict × Agent 命中率"),
            mo.ui.table(_va, pagination=False),
            mo.md("### 置信度分层校准（5 日窗口；high≥0.6 / mid 0.4~0.6 / low<0.4）"),
            mo.ui.table(_tier, pagination=False),
            mo.md(f"### 明细（最近 100 条，共 {_total} 条建档）"),
            mo.ui.table(_det, pagination=False),
        ])
    _retro


@app.cell
def _(C, mo):
    EX = C.fetch_df("""
        SELECT period_end, total_recommendations, executed_entries, entry_rate,
               win_rate, exit_accuracy, total_return
        FROM execution_stats WHERE period = 'daily'
        ORDER BY period_end DESC LIMIT ?""", (C.window_days(5),))
    return (EX,)


@app.cell
def _(EX, mo):
    mo.vstack([
        mo.md(f"## 执行统计（execution_stats，period=daily 最近 {C.window_days(5)} 期）"),
        mo.ui.table(EX, pagination=False) if not EX.empty else mo.md("execution_stats 空表。"),
    ])


if __name__ == "__main__":
    app.run()
