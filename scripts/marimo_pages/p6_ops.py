import os
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
    import datetime as dt
    import shutil
    from pathlib import Path as _P
    return C, alt, dt, mo, shutil, _P


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
    mo.md("# P6 运维全景\n与 Bitable T1/T2 同源（pipeline_runs 14 作业白名单），此页为深钻版。")


@app.cell
def _(ASOF, C, dt, mo):
    _runs_win = C.window_days(13)  # #76：daily=13 / weekly=13（floor≥7 无感）/ monthly=30
    _cutoff = (dt.date.fromisoformat(ASOF) - dt.timedelta(days=_runs_win)).isoformat()
    RUNS = C.fetch_df(f"""
        SELECT date, pipeline_name AS job,
               CASE WHEN SUM(status='failed')  > 0 THEN 'failed'
                    WHEN SUM(status='success') > 0 THEN 'success'
                    ELSE 'skipped' END AS status,
               ROUND(MAX(duration_s) / 60.0, 1) AS dur_min
        FROM pipeline_runs
        WHERE date >= ? AND pipeline_name IN ({','.join('?' * len(C.JOBS))})
        GROUP BY date, pipeline_name""", (_cutoff, *C.JOBS))
    return (RUNS,)


@app.cell
def _(C, RUNS, mo):
    if RUNS.empty:
        _view = mo.md("## 运行矩阵\n近 14 日无 pipeline_runs 记录。")
    else:
        _colors = {"success": "#dafbe1", "failed": "#ffebe9",
                   "skipped": "#fff8c5", "": "#f6f8fa"}
        _marks = {"success": "✅", "failed": "❌", "skipped": "⏭", "": "·"}
        _piv = RUNS.pivot_table(index="date", columns="job", values="status",
                                aggfunc="first").reindex(columns=C.JOBS).fillna("")
        _durs = RUNS.pivot_table(index="date", columns="job", values="dur_min",
                                 aggfunc="max").reindex(columns=C.JOBS)
        _head = "".join(f"<th style='padding:4px 6px;font-size:11px;'>{j}</th>" for j in C.JOBS)
        _body = []
        for _d in sorted(_piv.index, reverse=True):
            _cells = []
            for _j in C.JOBS:
                _st = _piv.at[_d, _j]
                _dm = _durs.at[_d, _j]
                _tip = f"{_d} {_j}: {_st or '未运行'}" + (f"（{_dm}min）" if _dm == _dm else "")
                _cells.append(
                    f"<td title='{_tip}' style='background:{_colors[_st]};text-align:center;"
                    f"padding:3px 5px;font-size:11px;'>{_marks[_st]}</td>")
            _body.append(f"<tr><td style='padding:3px 6px;font-size:11px;white-space:nowrap;'>"
                         f"{_d}</td>{''.join(_cells)}</tr>")
        _view = mo.vstack([
            mo.md(f"## 14 作业 × 近 {C.window_days(13)} 日运行矩阵（✅成功 ❌失败 ⏭跳过 ·未运行；悬停看耗时）"),
            mo.md("<div style='overflow-x:auto;'><table style='border-collapse:collapse;'>"
                    f"<tr><th></th>{_head}</tr>{''.join(_body)}</table></div>"),
        ])
    _view


@app.cell
def _(ASOF, C, alt, mo):
    _ddp_win = C.window_days(14)  # #76：daily=14 / weekly=14（floor≥7 无感）/ monthly=30
    DDP = C.fetch_df("""
        SELECT date, ROUND(MAX(duration_s) / 60.0, 1) AS dur_min
        FROM pipeline_runs
        WHERE pipeline_name = 'daily_data_pipeline' AND date >= date(?, ?)
        GROUP BY date ORDER BY date""", (ASOF, f"-{_ddp_win} day"))
    return (DDP,)


@app.cell
def _(DDP, alt, mo):
    if DDP.empty:
        _view = mo.md("## 主管道耗时\n近期无 daily_data_pipeline 记录。")
    else:
        _chart = alt.Chart(DDP).mark_line(point=True).encode(
            x=alt.X("date:O", title="日期"),
            y=alt.Y("dur_min:Q", title="耗时（分钟）"),
            tooltip=["date", "dur_min"])
        _view = mo.vstack([
            mo.md(f"## daily_data_pipeline 耗时趋势（近 {C.window_days(14)} 日）"),
            mo.ui.altair_chart(_chart),
        ])
    _view


@app.cell
def _(C, mo):
    _dl_win = C.window_days(7)  # #76：daily=7 / weekly=7 / monthly=30
    DL = C.fetch_df(f"""
        SELECT content_type, channel, COUNT(*) AS n,
               SUM(status = 'success') AS ok
        FROM delivery_log WHERE created_at >= date('now', ?)
        GROUP BY content_type, channel ORDER BY n DESC""", (f"-{_dl_win} day",))
    AL = C.fetch_df("""
        SELECT severity, COUNT(*) AS n FROM alerts
        WHERE status = 'open' GROUP BY severity ORDER BY n DESC""")
    return AL, DL


@app.cell
def _(AL, DL, C, mo):
    mo.vstack([
        mo.md(f"## 投递成功率（delivery_log 近 {C.window_days(7)} 日，按 content_type × channel）"),
        mo.ui.table(DL, pagination=False) if not DL.empty else mo.md("近期无投递记录。"),
        mo.md("## 未决告警（alerts status=open，按严重度）"),
        mo.ui.table(AL, pagination=False) if not AL.empty else mo.md("无未决告警。"),
    ])


@app.cell
def _(ASOF, dt, mo, shutil, _P):
    try:
        _free_gb = round(shutil.disk_usage(os.path.expanduser("~/qs")).free / 1024**3, 1)
        _disk = f"磁盘剩余 **{_free_gb} GB**（/home 分区）"
    except Exception as _e:
        _disk = f"磁盘探测失败: {_e}"
    try:
        _wd = _P(os.path.expanduser("~/qs/logs/watchdog.jsonl"))
        _today = dt.date.fromisoformat(ASOF).isoformat()
        _n_today = _n_total = 0
        if _wd.exists():
            _lines = _wd.read_text(encoding="utf-8", errors="replace").splitlines()
            _n_total = len(_lines)
            _n_today = sum(1 for _l in _lines[-2000:] if _today in _l)
        _dog = f"watchdog.jsonl 共 {_n_total} 条事件，其中含 {_today} 的最近 2000 条内 {_n_today} 条"
    except Exception as _e:
        _dog = f"watchdog 日志读取失败: {_e}"
    mo.md(f"## 主机状态\n- {_disk}\n- {_dog}")


if __name__ == "__main__":
    app.run()
