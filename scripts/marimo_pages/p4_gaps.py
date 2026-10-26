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
    import pandas as pd
    return C, mo, pd


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
    mo.md("# P4 回测与纸面交易 —— 缺口占位页\n"
          "**本页不伪造数据**：下表行数为渲染时实测。建了没通电的表如实显示 0 行 + 原因 + 卡号，"
          "通电后此页自动长出真数据。")


@app.cell
def _(C, mo, pd):
    _rows = []
    for _t, (_reason, _ticket) in C.GAP_TABLES.items():
        try:
            _n = C.one(f"SELECT COUNT(*) FROM {_t}")[0]
            _status = "🔴 零记录（缺口）" if _n == 0 else f"⚠️ 已有 {_n} 行（与缺口清单不符，需核查接线）"
        except Exception as _e:
            _n, _status = -1, f"❓ 查询失败: {_e}"
        _rows.append({"表": _t, "实测行数": _n, "状态": _status,
                      "原因": _reason, "对应卡号": _ticket})
    GAPS = pd.DataFrame(_rows)
    return (GAPS,)


@app.cell
def _(GAPS, mo):
    mo.vstack([
        mo.md("## 缺口清单（渲染时实测行数）"),
        mo.ui.table(GAPS, pagination=False),
    ])


@app.cell
def _(C, mo):
    PTS = C.fetch_df("""
        SELECT date, total_signals, filled_signals, avg_slippage, max_slippage,
               paper_return, benchmark_return
        FROM paper_trading_stats ORDER BY date DESC LIMIT 5""")
    return (PTS,)


@app.cell
def _(PTS, mo):
    mo.vstack([
        mo.md("## paper_trading_stats 快照（唯一有数的纸面交易表，历史仅 1 行级）"),
        mo.ui.table(PTS, pagination=False) if not PTS.empty else mo.md("paper_trading_stats 也无数据。"),
        mo.md("**验收路径**：paper_trading 验收卡销案后，本页升级为真实净值曲线 + 基准对比。"),
    ])


if __name__ == "__main__":
    app.run()
