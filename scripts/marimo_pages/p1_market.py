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
    import pandas as pd
    return C, alt, mo, pd


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
    mo.md("# P1 市场环境")


@app.cell
def _(C, mo):
    REGIME = C.fetch_df("""
        SELECT date, regime, confidence, avg_change, breadth, volume_score
        FROM daily_regime ORDER BY date DESC LIMIT 5""")
    return (REGIME,)


@app.cell
def _(REGIME, mo):
    mo.vstack([
        mo.md("## 市场状态（daily_regime 最近 5 日）"),
        mo.ui.table(REGIME, pagination=False) if not REGIME.empty else mo.md("daily_regime 空表。"),
    ])


@app.cell
def _(C, mo):
    SENT = C.fetch_df("""
        SELECT date, zt_count, dt_count, up_count, down_count
        FROM market_sentiment ORDER BY date DESC LIMIT 10"""
    ).iloc[::-1].reset_index(drop=True)
    return (SENT,)


@app.cell
def _(SENT, alt, mo, pd):
    if SENT.empty:
        _view = mo.md("## 涨跌停情绪\nmarket_sentiment 空表。")
    else:
        _long = SENT.melt(id_vars=["date"], value_vars=["zt_count", "dt_count"],
                          var_name="type", value_name="count")
        _long["type"] = _long["type"].map({"zt_count": "涨停", "dt_count": "跌停"})
        _chart = alt.Chart(_long).mark_bar().encode(
            x=alt.X("date:O", title="日期"), y=alt.Y("count:Q", title="家数"),
            color="type:N", tooltip=["date", "type", "count"])
        _view = mo.vstack([
            mo.md("## 涨跌停情绪（market_sentiment 近 10 日）\n"
                  "注：market_temp 列实测恒 0.0（死列），改用涨停/跌停家数呈现。"),
            mo.ui.altair_chart(_chart),
        ])
    _view


@app.cell
def _(ASOF, C, alt, mo):
    _win = C.window_days(40)  # #76：daily=40 / weekly=40 / monthly=40（大窗不动）
    IDX = C.fetch_df("""
        SELECT date, name, close FROM index_daily
        WHERE code IN ('000300.SH','399006.SZ') AND date >= date(?, ?)
        ORDER BY date""", (ASOF, f"-{_win} day"))
    return (IDX,)


@app.cell
def _(ASOF, IDX, alt, mo):
    if IDX.empty:
        _view = mo.md("## 指数走势\nindex_daily 无数据。")
    else:
        _mx = str(IDX["date"].max())
        _note = (f"\n⚠️ 指数数据停在 {_mx}（早于数据截至日 {ASOF}），走势仅供历史参考。"
                 if _mx < ASOF else "")
        _chart = alt.Chart(IDX).mark_line(point=True).encode(
            x=alt.X("date:O", title="日期"), y=alt.Y("close:Q", title="收盘"),
            color="name:N", tooltip=["date", "name", "close"])
        _view = mo.vstack([
            mo.md(f"## 指数走势（沪深300 / 创业板指，近 {C.window_days(40)} 交易日）\n"
                  "index_daily 实测仅含 000300.SH 与 399006.SZ（无上证指数）。" + _note),
            mo.ui.altair_chart(_chart),
        ])
    _view


@app.cell
def _(C, alt, mo):
    LAD = C.fetch_df("""
        SELECT trade_date, board_level, COUNT(*) AS n
        FROM limit_up_ladder
        WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM limit_up_ladder)
        GROUP BY trade_date, board_level ORDER BY trade_date, board_level""")
    return (LAD,)


@app.cell
def _(LAD, alt, mo):
    if LAD.empty:
        _view = mo.md("## 涨停梯队\nlimit_up_ladder 空表。")
    else:
        _chart = alt.Chart(LAD).mark_bar().encode(
            x=alt.X("trade_date:O", title="交易日"), y=alt.Y("n:Q", title="家数"),
            color="board_level:N", tooltip=["trade_date", "board_level", "n"])
        _view = mo.vstack([
            mo.md("## 涨停梯队（最新快照，按连板层级）"),
            mo.ui.altair_chart(_chart),
        ])
    _view


@app.cell
def _(ASOF, C, mo):
    SFF = C.fetch_df("""
        SELECT sector_name, main_net_amount, change_pct, top_stock
        FROM sector_fund_flow
        WHERE date = (SELECT MAX(date) FROM sector_fund_flow)
        ORDER BY main_net_amount DESC LIMIT 10""")
    SFF_DATE = C.one("SELECT MAX(date) FROM sector_fund_flow")
    return SFF, SFF_DATE


@app.cell
def _(ASOF, SFF, SFF_DATE, mo):
    if SFF.empty:
        _view = mo.md("## 板块资金流\nsector_fund_flow 空表。")
    else:
        _d = str(SFF_DATE[0]) if SFF_DATE else "?"
        _df = SFF.copy()
        _df["main_net_amount"] = (_df["main_net_amount"] / 1e8).round(2)
        _df = _df.rename(columns={"main_net_amount": "主力净流入(亿)",
                                  "change_pct": "涨跌幅%", "sector_name": "板块",
                                  "top_stock": "领涨股"})
        _stale = (f"\n⚠️ **数据停在 {_d}（早于 {ASOF}），以下为历史快照，标灰谨慎引用。**"
                  if _d < ASOF else f"\n数据日 {_d}。")
        _view = mo.vstack([
            mo.md("## 板块资金流（概念板块，主力净流入 Top10）" + _stale),
            mo.ui.table(_df, pagination=False),
        ])
    _view


if __name__ == "__main__":
    app.run()
