"""P2 持仓明细 — marimo 独立页 (09-29 02:00 窗, Day1 mock P2 落地)。

数据源: paper_positions_snapshot (34 行, 纸面交易持仓) + stock_pool active
(执行层持仓, 统筹裁 a52d1dab 两段并列)。
与 p3_performance 区分: p3 是「业绩指标」, p2 是「持仓明细」
(用户视角席 qlib 教训: 信号质量 vs 组合表现两个独立打分板分开)。
每行: code/name/shares/mv/weight/entry_price/unrealized_pnl/entry_date/holding_days。
只读 (铁律 #4)。
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
    mo.md("# P2 持仓明细\n纸面交易持仓（组合表现板）——与 p1 推荐池（信号质量板）分开。")


@app.cell
def _(C):
    try:
        # entry_date/holding_days 不在 paper_positions_snapshot schema
        # (cols=date/code/shares/mv/weight/entry_price/unrealized_pnl/created_at)
        # — join stock_pool 补入场日, holding_days 现算。
        SNAP = C.fetch_df("""
            SELECT p.date AS 快照日, p.code AS 代码,
                   IFNULL(si.name, p.code) AS 名称,
                   p.shares AS 股数, p.mv AS 市值, p.weight AS 权重,
                   p.entry_price AS 入场价, p.unrealized_pnl AS 浮动盈亏,
                   sp.entry_date AS 入场日,
                   CAST(julianday(p.date) - julianday(sp.entry_date) AS INTEGER) AS 持有天数
            FROM paper_positions_snapshot p
            LEFT JOIN stock_info si ON si.code = p.code
            LEFT JOIN stock_pool sp ON sp.code = p.code AND sp.status = 'active'
            WHERE p.date = (SELECT MAX(date) FROM paper_positions_snapshot)
            ORDER BY p.mv DESC""")
    except Exception:
        SNAP = None
    return (SNAP,)


@app.cell
def _(SNAP, mo):
    if SNAP is not None and len(SNAP):
        _d = SNAP["快照日"].iloc[0]
        mo.vstack([
            mo.md(f"### 📸 纸面持仓快照（{_d}，{len(SNAP)} 只，按市值降序）"),
            mo.ui.table(SNAP, pagination=True),
        ])
    else:
        mo.md("### 📸 纸面持仓快照\n暂无快照（paper_trading 未启用或未跑）。")


@app.cell
def _(SNAP, mo):
    if SNAP is not None and len(SNAP):
        _total_mv = float(SNAP["市值"].sum())
        _total_pnl = float(SNAP["浮动盈亏"].sum())
        _pnl_sign = "+" if _total_pnl >= 0 else ""
        mo.vstack([
            mo.md("### 📊 组合概览"),
            mo.ui.table([{
                "总市值": f"{_total_mv:,.0f}",
                "总浮动盈亏": f"{_pnl_sign}{_total_pnl:,.0f}",
                "持仓数": len(SNAP),
            }], pagination=False),
        ])
    else:
        mo.md("")


@app.cell
def _(C):
    try:
        ACTIVE = C.fetch_df("""
            SELECT sp.code AS 代码, IFNULL(si.name, sp.name) AS 名称,
                   sp.entry_date AS 入场日, sp.entry_price AS 入场价,
                   sp.last_price AS 现价,
                   ROUND((sp.last_price - sp.entry_price) / sp.entry_price * 100, 2) AS 浮动盈亏pct,
                   sp.stop_loss AS 参考卖出线, sp.take_profit AS 目标位
            FROM stock_pool sp
            LEFT JOIN stock_info si ON si.code = sp.code
            WHERE sp.status = 'active'
            ORDER BY sp.entry_date DESC""")
    except Exception:
        ACTIVE = None
    return (ACTIVE,)


@app.cell
def _(ACTIVE, mo):
    if ACTIVE is not None and len(ACTIVE):
        mo.vstack([
            mo.md(f"### 💼 执行层持仓（stock_pool active，{len(ACTIVE)} 只）"),
            mo.ui.table(ACTIVE, pagination=True),
        ])
    else:
        mo.md("### 💼 执行层持仓\n暂无 active 持仓。")


@app.cell
def _(ASOF, mo):
    mo.md(f"---\n数据截至 **{ASOF}**；缺口与陈旧项见各页水印（铁律 #12）。")


if __name__ == "__main__":
    app.run()
