# -*- coding: utf-8 -*-
"""chart_reader Leg B — LLM 读图腿 + ATR 护栏 (#101 切2)

设计稿 §3 Leg B:
- 输入 = 60日 OHLCV 结构化摘要 (数字非图片) + Leg A levels + 现ATR带
- LLM 通道 = app.agent.get_llm_service().call_structured (AG-10 统一入口)
  → chat_structured 2-attempt + dataclass 强校验 (先例 client.py:430)
- ATR 护栏 (确定性底线): entry_zone ⊆ [price±0.5ATR] / stop 距 entry ∈[1,3]×ATR /
  profit ≥ entry+2×ATR / conf∈[0,1] / schema 不过 — 违规 → fallback 纯公式值+degraded

LLM 永不进打分公式; 09 只写 chart_analysis (§1.5: signal/pending_entry 唯一写者=02)。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from agent_layer.chart_reader.levels import build_levels
from agent_layer.chart_reader.repository import (
    ATR_ENTRY_MULT,
    ATR_PROFIT_MULT,
    ATR_STOP_MULT,
    ChartAnalysisRepository,
)

SUMMARY_WINDOW_DAYS = 60
ATR_WINDOW = 14

# 护栏窗口 (§3 确定性底线, 价格单位乘数)
GUARD_ENTRY_BAND = ATR_ENTRY_MULT          # 0.5: entry_zone ⊆ price ± 0.5×ATR
GUARD_STOP_MIN = 1.0                        # stop 距 entry_mid ≥ 1×ATR
GUARD_STOP_MAX = 3.0                        # §3 护栏窗上界 (勿混 02 公式乘数 2.0)
GUARD_PROFIT_MIN = 2.0                      # profit − entry_mid ≥ 2×ATR


@dataclass
class ChartConclusion:
    """§3 输出 schema — chat_structured dataclass 强校验面"""

    pattern: str
    entry_zone: list = field(default_factory=list)   # [low, high]
    stop: float = 0.0
    profit: float = 0.0
    confidence: float = 0.0
    reasons: list = field(default_factory=list)


def compute_atr(rows) -> float | None:
    """ATR = 14窗 (high-low) 均值 round4 (02 同式); 不足14根 → None (宁缺毋滥)。"""
    if len(rows) < ATR_WINDOW:
        return None
    window = rows[-ATR_WINDOW:]
    return round(sum(r[2] - r[3] for r in window) / ATR_WINDOW, 4)


def build_ohlcv_summary(rows, limit: int = SUMMARY_WINDOW_DAYS) -> dict:
    """60日结构化摘要: 窗外截断; 数字面 = window_rows + 聚合指标 (非图片)。"""
    win = rows[-limit:]
    return {
        "window_days": len(win),
        "rows": [[r[0], r[4], r[2], r[3], r[5]] for r in win],  # date,close,high,low,volume
        "last_close": win[-1][4],
        "window_high": max(r[2] for r in win),
        "window_low": min(r[3] for r in win),
        "avg_volume": round(sum(r[5] for r in win) / len(win), 2),
    }


def validate_conclusion(conclusion: ChartConclusion | None,
                        current_price: float, atr: float) -> dict:
    """ATR 护栏五规 (§3 确定性底线)。

    Returns {"ok", "violations", "values"}:
    - ok=True → values = LLM 值原样 (entry/stop/profit/confidence)
    - ok=False → values = 纯 ATR 公式值 (02 同式) + confidence=None (degraded)
    """
    formula = {
        "entry_low": round(current_price - GUARD_ENTRY_BAND * atr, 2),
        "entry_high": round(current_price + GUARD_ENTRY_BAND * atr, 2),
        "stop": round(current_price - ATR_STOP_MULT * atr, 2),
        "profit": round(current_price + ATR_PROFIT_MULT * atr, 2),
        "confidence": None,
    }
    violations: list[str] = []
    if conclusion is None or not isinstance(conclusion, ChartConclusion):
        return {"ok": False, "violations": ["schema_fail"], "values": formula}

    mid = sum(conclusion.entry_zone) / 2
    lo_band = current_price - GUARD_ENTRY_BAND * atr
    hi_band = current_price + GUARD_ENTRY_BAND * atr
    if not conclusion.entry_zone or not (
        conclusion.entry_zone[0] >= lo_band and conclusion.entry_zone[1] <= hi_band
    ):
        violations.append("entry_out_of_band")
    if conclusion.stop >= mid:
        violations.append("stop_above_entry")
    else:
        gap = mid - conclusion.stop
        if gap < GUARD_STOP_MIN * atr:
            violations.append("stop_too_close")
        elif gap > GUARD_STOP_MAX * atr:
            violations.append("stop_too_far")
    if conclusion.profit - mid < GUARD_PROFIT_MIN * atr:
        violations.append("profit_too_small")
    if not (0.0 <= conclusion.confidence <= 1.0):
        violations.append("confidence_out_of_range")

    if violations:
        return {"ok": False, "violations": violations, "values": formula}
    return {
        "ok": True,
        "violations": [],
        "values": {
            "entry_low": conclusion.entry_zone[0],
            "entry_high": conclusion.entry_zone[1],
            "stop": conclusion.stop,
            "profit": conclusion.profit,
            "confidence": conclusion.confidence,
        },
    }


def load_candidates(conn, date: str) -> list[str]:
    """作用域 §4: signal_provenance 当日 ∪ stock_pool 未出场 (SQL 即跨层界面)。"""
    rows = conn.execute(
        "SELECT DISTINCT code FROM signal_provenance WHERE date = ? "
        "UNION "
        "SELECT code FROM stock_pool WHERE status NOT IN ('exited')",
        (date,),
    ).fetchall()
    return sorted(r[0] for r in rows)


class ChartLLMAnalyzer:
    """每候选: LegA levels + ATR + LLM 读图 → chart_analysis 双录 (atr_floor + llm_chart)。"""

    def __init__(self, db, llm_service=None):
        self.db = db
        self.repo = ChartAnalysisRepository(db)
        self.llm_service = llm_service

    def analyze(self, date: str) -> dict:
        """跑当日候选集。返回 {"analyzed", "llm_ok", "degraded"} (铁律#12: 可观测)。"""
        conn = self.db.get_connection()
        analyzed = llm_ok = degraded = 0
        for code in load_candidates(conn, date):
            rows = conn.execute(
                "SELECT date, open, high, low, close, volume FROM daily_klines "
                "WHERE code=? AND date<=? ORDER BY date",
                (code, date),
            ).fetchall()
            if not rows or rows[-1][0] != date:
                continue   # 当日无K线 → 跳过不计 (宁缺毋滥)
            price = rows[-1][4]
            atr = compute_atr(rows)
            if atr is None or atr <= 0:
                continue   # ATR 不可得 → 不硬造 (02 的 2% 退化路径不镜像)
            levels = build_levels(code, date, conn, atr=atr) or {}

            # LegA 基线行 (atr_floor): 切1 同款公式带
            self.repo.write_baseline(code, date, levels,
                                     current_price=price, atr=atr)
            analyzed += 1

            # LegB LLM 行 (llm_chart): 合法→LLM值; 违规/关LLM→degraded 公式值
            conclusion, raw = self._call_llm(code, date, rows, price, atr, levels)
            verdict = validate_conclusion(conclusion, price, atr)
            if verdict["ok"]:
                llm_ok += 1
                reason_list = [str(r) for r in conclusion.reasons]
                pattern, confidence = conclusion.pattern, conclusion.confidence
            else:
                degraded += 1
                reason_list = ["degraded:" + v for v in verdict["violations"]]
                pattern, confidence = None, None
            self.repo.write_llm(
                code, date, pattern=pattern,
                entry_low=verdict["values"]["entry_low"],
                entry_high=verdict["values"]["entry_high"],
                stop=verdict["values"]["stop"], profit=verdict["values"]["profit"],
                confidence=confidence, reasons=reason_list,
                levels=levels, atr=atr, llm_raw=raw,
            )
        return {"analyzed": analyzed, "llm_ok": llm_ok, "degraded": degraded}

    def _call_llm(self, code, date, rows, price, atr, levels):
        """AG-10 通道: service.call_structured(prompt, ChartConclusion, ...)。失败 → (None, None)。"""
        if self.llm_service is None:
            return None, None
        summary = build_ohlcv_summary(rows)
        bars = "\n".join(
            f"{r[0]} 收{r[1]} 高{r[2]} 低{r[3]} 量{r[4]}"
            for r in summary["rows"]
        )
        prompt = (
            f"股票 {code} ({date}) 日线读图。"
            f"近60日OHLCV (窗口=60日, 实际{summary['window_days']}根):\n{bars}\n"
            f"聚合: 收盘 {summary['last_close']} / 窗口高 {summary['window_high']} / "
            f"窗口低 {summary['window_low']} / 日均量 {summary['avg_volume']}。\n"
            f"规则腿支撑压力位: S1={levels.get('S1')} S2={levels.get('S2')} "
            f"R1={levels.get('R1')} R2={levels.get('R2')} "
            f"(vol_ratio={levels.get('vol_ratio')}, "
            f"pullback_vol_ratio={levels.get('pullback_vol_ratio')}, note={levels.get('note')})。\n"
            f"当前价 {price}, ATR={atr}。\n"
            f"ATR 护栏 (必须遵守): 入场区间须在 [{round(price - GUARD_ENTRY_BAND * atr, 2)}, "
            f"{round(price + GUARD_ENTRY_BAND * atr, 2)}] 内; "
            f"止损距入场中点 {GUARD_STOP_MIN}~{GUARD_STOP_MAX}×ATR 且在其下方; "
            f"止盈距入场中点 ≥{GUARD_PROFIT_MIN}×ATR。\n"
            f"请判断形态并给出 entry_zone[low,high] / stop / profit / confidence / reasons。"
        )
        try:
            conclusion = self.llm_service.call_structured(
                prompt, ChartConclusion,
                system="你是严谨的A股图表分析师。只输出符合schema的JSON。",
                call_point="chart_reader",
            )
        except Exception:
            return None, None
        if conclusion is None:
            return None, None
        return conclusion, json.dumps(asdict(conclusion), ensure_ascii=False)
