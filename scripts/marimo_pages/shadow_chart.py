# -*- coding: utf-8 -*-
"""chart_analysis 影子模式展示函数 (#101 切3, p2_factors 展示位, 设计稿 §6)

自治拷贝声明: 评估口径与 src/agent_layer/chart_reader/evaluation.py 同源
同口径, 但本模块必须自治 — marimo 页运行时 = qs-marimo-venv, 无 qs src
可 import (页生态「独立拷贝不跨模块」先例); 口径变更须两处同改:
  evaluation.py = 喂 F2 的纯函数层 (测试面), 本模块 = 展示层 (export 面)。

口径 (与 evaluation.py 对齐, 变更须过统筹):
- 前视窗 EVAL_HORIZON_BARS=10 根日K;
- 同 bar 止损止盈双触 → 记止损先触 (保守口径);
- degraded 行 (llm_chart pattern IS NULL, 切2 判别键) 不入 LLM 组,
  单列 degraded_rows — 其数值即公式带, 入组只会稀释对照;
- 前视窗未满 → 率 None → 展示「—」(不造 0%);
- 表未迁移 (生产 v6 未跑) / 无数据 → 占位文案, export 不炸
  (全页全周期 export 铁律的前置防线)。

影子纪律 (§5): 裁决前 LLM 结论不入账 — 本模块零写 (纯只读)。
"""
from __future__ import annotations

import sqlite3

EVAL_HORIZON_BARS = 10   # = evaluation.py 同口径 (自治拷贝)

_SOURCES = ("atr_floor", "llm_chart")
_LABELS = {"atr_floor": "atr_floor（公式基线）",
           "llm_chart": "LLM（读图修正）"}


def _empty_group() -> dict:
    return {"rows": 0, "evaluated": 0, "entry_touch": 0,
            "stop_first": 0, "profit_first": 0,
            "entry_touch_rate": None, "stop_first_rate": None,
            "profit_first_rate": None}


def _forward_bars(conn, code: str, date: str) -> list:
    """图形日之后前 EVAL_HORIZON_BARS 根日K [(high, low), ...]; 表缺 → []。"""
    try:
        return conn.execute(
            "SELECT high, low FROM daily_klines "
            "WHERE code = ? AND date > ? ORDER BY date LIMIT ?",
            (code, date, EVAL_HORIZON_BARS)).fetchall()
    except sqlite3.OperationalError:
        return []


def _eval_row(fw, entry_low, entry_high, stop, profit):
    """单行三指标; 值面缺 (NULL) 或无前视窗 → None (不计 evaluated)。"""
    if not fw or None in (entry_low, entry_high, stop, profit):
        return None
    entry_touch = stop_first = profit_first = 0
    for high, low in fw:
        if low <= entry_high and high >= entry_low:   # bar 与入场带重叠
            entry_touch = 1
        if low <= stop:          # 同bar双触 → 止损先触 (保守口径)
            stop_first = 1
            break
        if high >= profit:
            profit_first = 1
            break
    return {"entry_touch": entry_touch, "stop_first": stop_first,
            "profit_first": profit_first}


def _group_rows(conn, src) -> list:
    cond = "source = ?"
    if src == "llm_chart":
        cond += " AND pattern IS NOT NULL"   # degraded 不入组 (切2 判别键)
    try:
        return conn.execute(
            "SELECT code, date, entry_low, entry_high, stop, profit "
            f"FROM chart_analysis WHERE {cond}", (src,)).fetchall()
    except sqlite3.OperationalError:
        return []            # 表未迁移 = 旧库形态, 展示层自护


def _degraded_count(conn) -> int:
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM chart_analysis "
            "WHERE source = 'llm_chart' AND pattern IS NULL").fetchone()[0]
    except sqlite3.OperationalError:
        return 0


def shadow_stats(conn) -> dict:
    """累计三率对照 (结构 = evaluation.compare_sources 同款, 喂展示层)。"""
    out = {s: _empty_group() for s in _SOURCES}
    out["degraded_rows"] = _degraded_count(conn)
    for src in _SOURCES:
        g = out[src]
        rows = _group_rows(conn, src)
        g["rows"] = len(rows)
        for code, d, elo, ehi, stop, prof in rows:
            m = _eval_row(_forward_bars(conn, code, d), elo, ehi, stop, prof)
            if m is None:
                continue
            g["evaluated"] += 1
            g["entry_touch"] += m["entry_touch"]
            g["stop_first"] += m["stop_first"]
            g["profit_first"] += m["profit_first"]
        if g["evaluated"]:
            g["entry_touch_rate"] = round(g["entry_touch"] / g["evaluated"], 4)
            g["stop_first_rate"] = round(g["stop_first"] / g["evaluated"], 4)
            g["profit_first_rate"] = round(g["profit_first"] / g["evaluated"], 4)
    return out


def _fmt_rate(r) -> str:
    return "—" if r is None else f"{r * 100:.1f}%"


_TD = "style='text-align:center;padding:3px 6px;font-size:12px;'"
_TH = "style='padding:3px 6px;font-size:12px;white-space:nowrap;'"


def shadow_display_html(conn) -> str:
    """影子段 HTML (标题+口径说明+对照表); 无表/无数据 → 占位文案。"""
    st = shadow_stats(conn)
    head = ("## 影子模式 · LLM 读图对照（6 周裁决窗三指标）\n"
            "裁决前 LLM 结论不入账（影子纪律 §5）；三率 = 入场区触达率 / "
            "止损先触率 / 止盈先触率，「—」= 前视窗未满不计。\n")
    if (st["atr_floor"]["rows"] == 0 and st["llm_chart"]["rows"] == 0
            and st["degraded_rows"] == 0):
        return (head + "\nchart_analysis 未迁移或无数据 — 表 v6 迁移 + "
                "读图管道通电后此段自动出现。")
    trs = []
    for src in _SOURCES:
        g = st[src]
        trs.append(
            "<tr>"
            f"<td {_TH}>{_LABELS[src]}</td>"
            f"<td {_TD}>{g['rows']}</td>"
            f"<td {_TD}>{g['evaluated']}</td>"
            f"<td {_TD}>{_fmt_rate(g['entry_touch_rate'])}</td>"
            f"<td {_TD}>{_fmt_rate(g['stop_first_rate'])}</td>"
            f"<td {_TD}>{_fmt_rate(g['profit_first_rate'])}</td>"
            "</tr>")
    tbl = ("<div style='overflow-x:auto;'><table style='border-collapse:collapse;'>"
           "<tr>"
           f"<th {_TH}>组</th><th {_TD}>行数</th><th {_TD}>已评估</th>"
           f"<th {_TD}>入场区触达率</th><th {_TD}>止损先触率</th>"
           f"<th {_TD}>止盈先触率</th></tr>"
           + "".join(trs) + "</table></div>"
           f"\n<br>degraded 行 {st['degraded_rows']} 条 — LLM 通道降级, "
           "不入组 (数值即公式带), 裁决日按证据聚合。")
    return head + tbl
