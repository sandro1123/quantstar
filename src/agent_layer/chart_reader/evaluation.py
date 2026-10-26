# -*- coding: utf-8 -*-
"""chart_reader 对照评估段 (#101 切3) — 影子模式三指标 (设计稿 §5)

先记录后对照: 双录已由切1/切2 落库 (atr_floor 基线 + llm_chart LLM修正);
本段 = 纯只读对照 — 6 周裁决窗三指标:
  入场区触达率 / 止损先触率 / 止盈先触率 (atr_floor vs llm_chart)。

影子纪律 (§5): 裁决前 LLM 结论不入账 — 本模块零写任何表
(含 signal/pending_entry, §1.5 唯一写者=02); 只读 chart_analysis +
daily_klines (total_changes 可验, fcdb 探针先例)。

本卡裁定 (回执报统筹):
- 前视窗 EVAL_HORIZON_BARS=10 根日K (约两周);
- 同 bar 止损止盈双触 → 记止损先触 (保守口径);
- degraded 行 (llm_chart pattern IS NULL, 切2 判别键; atr_floor 行
  pattern 恒 NULL → 判别必须 source 门控) 不入 LLM 组, 单列
  degraded_rows — 其数值即公式带, 入组只会稀释对照;
- 前视窗空 (未到期) → 不计 evaluated, 率=None (不造 0%);
- daily_klines 缺席 → 评估静默降级 (rows 仍计) — 调用方
  (issues 哨兵 / p2 展示位) 各自另有表级自护, 本模块不重复兜底。
"""
from __future__ import annotations

import sqlite3

EVAL_HORIZON_BARS = 10   # 前视窗根数 (本卡裁定, 变更须过统筹)

_SOURCES = ("atr_floor", "llm_chart")


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


def compare_sources(conn, date: str | None = None) -> dict:
    """三指标对照 — 纯只读; date=None 累计 (喂F2稳定窗形态), 单日=快照。"""
    where, params = "", ()
    if date is not None:
        where, params = " AND date = ?", (date,)
    out = {s: _empty_group() for s in _SOURCES}
    out["degraded_rows"] = conn.execute(
        "SELECT COUNT(*) FROM chart_analysis "
        "WHERE source = 'llm_chart' AND pattern IS NULL" + where,
        params).fetchone()[0]
    for src in _SOURCES:
        cond = "source = ?"
        if src == "llm_chart":
            cond += " AND pattern IS NOT NULL"   # degraded 不入组 (切2 判别键)
        rows = conn.execute(
            "SELECT code, date, entry_low, entry_high, stop, profit "
            f"FROM chart_analysis WHERE {cond}" + where,
            (src, *params)).fetchall()
        g = out[src]
        g["rows"] = len(rows)
        for code, d, elo, ehi, st, pf in rows:
            m = _eval_row(_forward_bars(conn, code, d), elo, ehi, st, pf)
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
