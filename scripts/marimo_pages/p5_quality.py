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
    mo.md("# P5 数据新鲜度与质量\n总控台单一事实源的镜像：缺口与停滞在这里集中可见。")


@app.cell
def _(C, mo):
    FA = C.fetch_df("""
        SELECT table_name, expected_by, actual_at, delay_min, status
        FROM freshness_audit
        WHERE date = (SELECT MAX(date) FROM freshness_audit)
        ORDER BY status, table_name""")
    FA_DATE = C.one("SELECT MAX(date) FROM freshness_audit")
    return FA, FA_DATE


@app.cell
def _(FA, FA_DATE, mo):
    _d = str(FA_DATE[0]) if FA_DATE else "?"
    mo.vstack([
        mo.md(f"## 新鲜度审计（freshness_audit，审计日 {_d}）"),
        mo.ui.table(FA, pagination=False) if not FA.empty else mo.md("freshness_audit 空表。"),
    ])


@app.cell
def _(C, mo):
    QC = C.fetch_df("""
        SELECT date, table_name, check_name, level, result, detail
        FROM quality_checks ORDER BY date DESC, table_name""")
    return (QC,)


@app.cell
def _(QC, mo):
    mo.vstack([
        mo.md("## 质量检查（quality_checks，全表实测仅个位数行——历史积累薄，本身即缺口）"),
        mo.ui.table(QC, pagination=False) if not QC.empty else mo.md("quality_checks 空表。"),
    ])


@app.cell
def _(ASOF, C, mo):
    _win = C.window_days(7)  # #76：daily=7 / weekly=7 / monthly=30
    FLG = C.fetch_df("""
        SELECT flag, COUNT(*) AS n, COUNT(DISTINCT table_name) AS tables
        FROM data_quality_flags WHERE date >= date(?, ?)
        GROUP BY flag ORDER BY n DESC""", (ASOF, f"-{_win} day"))
    FLG_TOP = C.fetch_df("""
        SELECT table_name, COUNT(*) AS n FROM data_quality_flags
        WHERE date >= date(?, ?)
        GROUP BY table_name ORDER BY n DESC LIMIT 10""", (ASOF, f"-{_win} day"))
    return FLG, FLG_TOP


@app.cell
def _(FLG, FLG_TOP, mo):
    mo.vstack([
        mo.md(f"## 质量旗标（data_quality_flags 近 {C.window_days(7)} 日）"),
        mo.md("**按旗标类型**"),
        mo.ui.table(FLG, pagination=False) if not FLG.empty else mo.md("近期无旗标。"),
        mo.md("**按表 Top10**"),
        mo.ui.table(FLG_TOP, pagination=False) if not FLG_TOP.empty else mo.md("无数据。"),
    ])


@app.cell
def _(C, mo):
    SN = C.one("SELECT COUNT(*), SUM(code IS NULL OR code = '') FROM stock_news")
    return (SN,)


@app.cell
def _(SN, mo):
    if SN and SN[0]:
        _n, _bad = SN[0], SN[1] or 0
        _msg = (f"stock_news 实测 {int(_n)} 行，其中 code 为空 **{int(_bad)}** 行"
                f"（{'全部——策略 A 硬伤 Q-53 在办' if _bad == _n else '部分'}）；"
                "无个股归属的新闻无法进因子链。")
    else:
        _msg = "stock_news 空表或查询失败。"
    mo.vstack([
        mo.md("## 已知质量硬伤（实测复核）"),
        mo.md(f"- {_msg}"),
        mo.md("- quality_checks / health_history 历史积累薄（见上）。"),
    ])


@app.cell
def _(C, mo, pd):
    GAP_LIST = pd.DataFrame([
        {"表": t, "原因": r, "对应卡号": k}
        for t, (r, k) in C.GAP_TABLES.items()])
    return (GAP_LIST,)


@app.cell
def _(GAP_LIST, STALE, mo):
    mo.vstack([
        mo.md("## 缺口清单（建了没通电；实测行数见 P4 页）"),
        mo.ui.table(GAP_LIST, pagination=False),
        mo.md("## 陈旧监视表（水印同源；落后于数据截至日即列出）\n- "
              + ("\n- ".join(STALE) if STALE else "无陈旧项。")),
    ])


if __name__ == "__main__":
    app.run()
