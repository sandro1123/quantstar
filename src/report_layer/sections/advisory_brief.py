"""卡VII 三时点投顾 — 公共函数层 + 三时点 Section (只读铁律)。

统筹裁定 sm-c525b45463d94435 三分叉:
1. 函数层 build_advisory_brief(conn, time_point) = section 与 CLI 共用的
   取数口径 (防口径分叉); 查询逻辑与 cli/advisory.py candidates/holdings 同源。
2. 三 section 搭晨/午既有管线 + 尾盘新管道 (14:52), 同链 report_layer
   build+push (qscard 不动 — 留给盘中触价卡)。
3. 三时点语义: morning=开盘前隔夜+昨收 / midday=上午实时 / tail=午后实时。

只读铁律: 本模块任何路径不得写库 (写库是 signal/paper 的事, 投顾只看不动)。
查询经同一 conn 只读执行; total_changes 探针在测试层断言零写。
"""
from __future__ import annotations

from common.logging import get_logger
from report_layer.sections.base import Section, SectionResult
from report_layer.sections.base import note_degraded

logger = get_logger("report.section.advisory_brief")

# 大盘 regime 中文化 (与 cli/advisory.py _REGIME_ZH 同口径)
_REGIME_ZH = {"bull": "偏多", "bear": "偏空", "neutral": "中性"}
# 现价距止损 5% 内 = 接近止损告警 (与 cli/advisory.py _NEAR_STOP_PCT 同)
_NEAR_STOP_PCT = 0.05
# 候选默认条数 (与 CLI candidates --top 默认一致)
_DEFAULT_TOP = 10

# 三时点标签 — 时点语义分层 (裁定③)
TIME_POINT_LABELS = {
    "morning": "晨间投顾 (开盘前: 隔夜市场 + 昨收视角)",
    "midday": "午间投顾 (上午盘面实时)",
    "tail": "尾盘投顾 (午后盘面实时)",
}


def _fetch_candidates(conn, top_n: int = _DEFAULT_TOP) -> list[dict]:
    """今日候选 topN — 只取最新评分日期, join 真名 + 在池标记 (CLI 同口径)。"""
    latest = conn.execute(
        "SELECT MAX(date) FROM model_predictions").fetchone()[0]
    if not latest:
        return []
    rows = conn.execute(
        "SELECT p.code, COALESCE(i.name, p.code) AS name, p.blended_score, "
        "CASE WHEN s.code IS NOT NULL THEN 1 ELSE 0 END AS in_pool "
        "FROM model_predictions p "
        "LEFT JOIN stock_info i ON i.code = p.code "
        "LEFT JOIN stock_pool s ON s.code = p.code "
        "WHERE p.date = ? AND p.blended_score IS NOT NULL "
        "ORDER BY p.blended_score DESC LIMIT ?", (latest, top_n)).fetchall()
    return [{"code": r["code"], "name": r["name"],
             "blended_score": r["blended_score"], "rank": i + 1,
             "in_pool": bool(r["in_pool"])}
            for i, r in enumerate(rows)]


def _fetch_holdings(conn, today: str | None = None) -> list[dict]:
    """持仓跟踪 — active 持仓 + 今日入场诚实 NULL (CLI holdings 同口径)。"""
    positions = []
    for r in conn.execute(
            "SELECT p.code, COALESCE(i.name, p.name) AS name, p.entry_price, "
            "p.last_price, p.position_qty, p.unrealized_pnl_pct, "
            "p.holding_days, p.stop_loss, p.dynamic_stop_loss, "
            "p.take_profit, p.entry_date FROM stock_pool p "
            "LEFT JOIN stock_info i ON i.code = p.code "
            "WHERE p.status = 'active' ORDER BY p.code"):
        note = ""
        lp = r["last_price"]
        pnl = r["unrealized_pnl_pct"]
        if today and str(r["entry_date"] or "")[:10] == today:
            # 范式7: 今日入场无新收盘价 → NULL + 提示, 不拿昨收冒充现价
            krow = conn.execute(
                "SELECT close FROM daily_klines WHERE code = ? AND date = ?",
                (r["code"], today)).fetchone()
            if krow and krow["close"] and r["entry_price"]:
                lp = krow["close"]
                pnl = round((lp / r["entry_price"] - 1) * 100, 2)
            else:
                lp = None
                pnl = None
                note = "今日入场,暂无最新收盘价,盈亏待结算"
        alert = ""
        stop = r["dynamic_stop_loss"] or r["stop_loss"]
        if stop and lp:
            if lp <= stop:
                alert = f"⚠️ 现价 {lp} 已触及参考卖出线 {stop}"
            elif (lp - stop) / lp <= _NEAR_STOP_PCT:
                alert = f"⚠️ 现价 {lp} 距参考卖出线 {stop} 不足 5%, 注意风险"
        positions.append({"code": r["code"], "name": r["name"],
                          "entry_price": r["entry_price"], "last_price": lp,
                          "qty": r["position_qty"],
                          "unrealized_pnl_pct": pnl,
                          "holding_days": r["holding_days"],
                          "stop_loss": r["stop_loss"],
                          "dynamic_stop_loss": r["dynamic_stop_loss"],
                          "take_profit": r["take_profit"],
                          "alert_zh": alert, "note_zh": note})
    return positions


def _fetch_regime(conn) -> str:
    """最新大盘 regime 中文化 (空库/异常 → 空串, 诚实降级不抛)。"""
    try:
        row = conn.execute(
            "SELECT regime FROM daily_regime ORDER BY date DESC LIMIT 1").fetchone()
    except Exception as _deg_exc:  # noqa: BLE001
        note_degraded("advisory_brief", _deg_exc)
        return ""
    if not row:
        return ""
    regime = row["regime"] if not isinstance(row, tuple) else row[0]
    if not isinstance(regime, str) or not regime:
        return ""
    return f"大盘{_REGIME_ZH.get(regime, regime)}"


def build_advisory_brief(conn, time_point: str, top_n: int = _DEFAULT_TOP,
                         today: str | None = None) -> dict:
    """三时点投顾取数函数层 (只读)。

    section 层与 CLI 共用此口径 — 防口径分叉 (裁定①)。
    任何时点都不得写库 (只读铁律, total_changes 探针在测试层断言)。
    """
    if time_point not in TIME_POINT_LABELS:
        raise ValueError(f"未知时点: {time_point!r}, 须为 {list(TIME_POINT_LABELS)}")
    candidates = _fetch_candidates(conn, top_n)
    holdings = _fetch_holdings(conn, today=today)
    regime_zh = _fetch_regime(conn)
    return {"time_point": time_point,
            "label_zh": TIME_POINT_LABELS[time_point],
            "candidates": candidates, "holdings": holdings,
            "regime_zh": regime_zh}


# ── 三时点 Section (搭 report_layer 既有管线) ────────────

class _AdvisorySectionBase(Section):
    """三时点共用骨架: 取数 → 渲染 → SectionResult (只读)。"""

    time_point: str = ""

    @property
    def name(self) -> str:
        return f"advisory_{self.time_point}"

    def _title(self, brief: dict) -> str:
        return f"🧭 {TIME_POINT_LABELS[self.time_point]}"

    def _render_body(self, brief: dict) -> str:
        lines = [f"  {brief['regime_zh']}"] if brief["regime_zh"] else []
        if not brief["candidates"] and not brief["holdings"]:
            # 诚实模板 (范式7): 两空时明示, 不静默降级
            lines.append("  📦 暂无候选与持仓数据 (待数据管道产出)")
            return "\n".join(lines)
        if brief["candidates"]:
            lines.append(f"  🎯 今日候选 Top{len(brief['candidates'])}")
            for c in brief["candidates"][:5]:
                pool_flag = " [已在池]" if c["in_pool"] else ""
                lines.append(f"    {c['rank']}. {c['code']} {c['name']} "
                             f"评分 {c['blended_score']:.3f}{pool_flag}")
        if brief["holdings"]:
            lines.append(f"  📦 推荐跟踪 {len(brief['holdings'])} 只")
            for h in brief["holdings"]:
                pnl = h["unrealized_pnl_pct"]
                pnl_s = f"{pnl:+.1f}%" if pnl is not None else "—"
                lp_s = h["last_price"] if h["last_price"] is not None else "—"
                lines.append(f"    {h['code']} {h['name']} 成本 {h['entry_price']} "
                             f"现价 {lp_s} 验证收益 {pnl_s} 持有 {h['holding_days']} 天")
                if h["note_zh"]:
                    lines.append(f"      ℹ️ {h['note_zh']}")
                if h["alert_zh"]:
                    lines.append(f"      {h['alert_zh']}")
        return "\n".join(lines)

    def build(self, context) -> SectionResult | None:
        try:
            db = getattr(context.data_layer, "db", None) if context.data_layer else None
            if db is None:
                return None
            conn = db.get_connection()
            brief = build_advisory_brief(conn, self.time_point, today=context.date)
            return SectionResult(
                title=self._title(brief),
                body=self._render_body(brief),
                subtitle=TIME_POINT_LABELS[self.time_point],
                name=self.name,
                sources=["qs.db: model_predictions/stock_pool/daily_regime"],
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"advisory section build failed: {e}")
            return None


class AdvisoryMorningSection(_AdvisorySectionBase):
    """晨间投顾 — 搭早报管线 (morning_brief 08:00): 隔夜+昨收视角。"""
    time_point = "morning"


class AdvisoryMiddaySection(_AdvisorySectionBase):
    """午间投顾 — 搭午报管线 (midday_brief 12:00): 上午实时。"""
    time_point = "midday"


class AdvisoryTailSection(_AdvisorySectionBase):
    """尾盘投顾 — 尾盘新管道 (14:52): 午后实时。"""
    time_point = "tail"
