"""策略A 热度衰减判据 — 5 路信号独立取值、独立测试 (派发 2026-08-24 交付物①)

设计约束 (回执 S-2):
- 无 IC 证据 → 不用固定绝对阈值; 计数型/比值型信号用显式声明参数 (可调、有负向对照),
  横截面型用当日分位排名。
- 每路信号独立函数, 单独取值单独测试; assess_heat 只做编排不糊黑盒分数。
- 数据缺失诚实降级: data_ok=False, 绝不造假值 (铁律 #12 可观测不可静默)。
- market_sentiment / hot_stock_history 薄表不作任何依据 (派发 §3 红线)。

信号极性:
- 衰减信号 (fired=True = 热度走弱): sector_outflow / stock_outflow_streak / volume_decay
- 支撑信号 (fired=True = 热度仍在): limit_pool_presence / lhb_frequency
"""
from __future__ import annotations

from dataclasses import dataclass, field

from common.logging import get_logger

logger = get_logger("strategy.lifecycle.heat")

# 信号名常量 — 上游引用这里, 不散落魔法字符串
SIG_SECTOR_OUTFLOW = "sector_outflow"
SIG_STOCK_OUTFLOW_STREAK = "stock_outflow_streak"
SIG_VOLUME_DECAY = "volume_decay"
SIG_LIMIT_POOL = "limit_pool_presence"
SIG_LHB_FREQ = "lhb_frequency"

DECAY_SIGNALS = (SIG_SECTOR_OUTFLOW, SIG_STOCK_OUTFLOW_STREAK, SIG_VOLUME_DECAY)
SUPPORT_SIGNALS = (SIG_LIMIT_POOL, SIG_LHB_FREQ)


@dataclass
class SignalResult:
    """单路信号取值结果。

    fired: 信号是否触发 (衰减信号触发=热度走弱; 支撑信号触发=热度仍在)
    value: 主观测值 (分位数/天数/比值/次数), 缺数据时为 None
    data_ok: False = 底层数据缺失或不足, 结论不可信 — fired 恒为 False 且不得据此决策
    """
    name: str
    fired: bool = False
    value: float | int | None = None
    data_ok: bool = True
    detail: dict = field(default_factory=dict)


def pct_rank(values: list[float], x: float) -> float | None:
    """x 在 values 中的分位排名 (严格小于 x 的占比, 0..1)。空列表 → None。"""
    if not values:
        return None
    below = sum(1 for v in values if v < x)
    return below / len(values)


def _rows(db, sql: str, params: tuple) -> list:
    conn = db.get_connection()
    return conn.execute(sql, params).fetchall()


# ---------------------------------------------------------------------------
# 信号 1: 板块主力净额转负 (横截面分位) — 个股行业映射用 stock_info.industry
# ---------------------------------------------------------------------------

def sector_outflow(db, code: str, as_of: str,
                   bottom_quantile: float = 0.30) -> SignalResult:
    """个股所属行业的当日主力净额是否转负且处于全行业尾部。

    归一化: 当日全部行业 main_net_amount 的分位排名 (回执 S-2 表第 1 行)。
    fired 条件: 净额 < 0 且 分位 <= bottom_quantile (默认 0.30, 无 IC 证据的声明参数)。
    只用行业 (industry), 不混概念: 一股对多概念会引入归因歧义。
    """
    r = SignalResult(name=SIG_SECTOR_OUTFLOW)
    ind_rows = _rows(db, "SELECT industry FROM stock_info WHERE code=?", (code,))
    if not ind_rows or not ind_rows[0]["industry"]:
        r.data_ok = False
        r.detail["reason"] = "no_industry_mapping"
        return r
    sector_name = ind_rows[0]["industry"]

    day_rows = _rows(
        db,
        "SELECT sector_name, main_net_amount FROM sector_fund_flow "
        "WHERE date=? AND sector_type='industry'",
        (as_of,),
    )
    if not day_rows:
        r.data_ok = False
        r.detail["reason"] = "no_sector_fund_flow_for_date"
        return r

    amounts = [row["main_net_amount"] or 0.0 for row in day_rows]
    mine = next((row["main_net_amount"] for row in day_rows
                 if row["sector_name"] == sector_name), None)
    if mine is None:
        r.data_ok = False
        r.detail["reason"] = f"sector_missing:{sector_name}"
        return r

    rank = pct_rank(amounts, mine)
    r.value = rank
    r.detail = {"sector": sector_name, "main_net_amount": mine, "quantile": bottom_quantile}
    r.fired = bool(mine < 0 and rank is not None and rank <= bottom_quantile)
    return r


# ---------------------------------------------------------------------------
# 信号 2: 个股连续净流出天数 (计数型)
# ---------------------------------------------------------------------------

def stock_outflow_streak(db, code: str, as_of: str,
                         min_days: int = 3) -> SignalResult:
    """截至 as_of 连续多少个「有数据的交易日」主力净流出。

    口径: 按 stock_moneyflow 中该股相邻记录连续计数 (表内记录天然是交易日序);
    当日净流入 >= 0 即清零。缺记录不算流出也不清零 (停牌/源缺失不伪造趋势)。
    fired 条件: streak >= min_days (默认 3, 计数型显式参数)。
    """
    r = SignalResult(name=SIG_STOCK_OUTFLOW_STREAK)
    rows = _rows(
        db,
        "SELECT main_net_inflow FROM stock_moneyflow "
        "WHERE code=? AND date<=? ORDER BY date DESC LIMIT 20",
        (code, as_of),
    )
    if not rows:
        r.data_ok = False
        r.detail["reason"] = "no_moneyflow_rows"
        return r

    streak = 0
    for row in rows:
        inflow = row["main_net_inflow"]
        if inflow is None:
            break
        if inflow < 0:
            streak += 1
        else:
            break
    r.value = streak
    r.detail = {"min_days": min_days}
    r.fired = streak >= min_days
    return r


# ---------------------------------------------------------------------------
# 信号 3: 成交量衰减 (时序自比)
# ---------------------------------------------------------------------------

def volume_decay(db, code: str, as_of: str,
                 ratio_threshold: float = 0.70) -> SignalResult:
    """当日成交量 / 自身近 5 日均量 (不含当日)。

    自比而非横截面: 量能衰减是个股自身节奏问题 (回执 S-2 表第 3 行)。
    fired 条件: ratio < ratio_threshold (默认 0.70)。不足 6 根 K 线 → data_ok=False。
    """
    r = SignalResult(name=SIG_VOLUME_DECAY)
    rows = _rows(
        db,
        "SELECT volume FROM daily_klines "
        "WHERE code=? AND date<=? ORDER BY date DESC LIMIT 6",
        (code, as_of),
    )
    if len(rows) < 6:
        r.data_ok = False
        r.detail = {"reason": f"klines_insufficient:{len(rows)}", "need": 6}
        return r

    volumes = [row["volume"] or 0.0 for row in rows]
    today_vol, base_vols = volumes[0], volumes[1:6]
    base_avg = sum(base_vols) / len(base_vols)
    if base_avg <= 0:
        r.data_ok = False
        r.detail = {"reason": "base_volume_zero"}
        return r

    ratio = today_vol / base_avg
    r.value = round(ratio, 4)
    r.detail = {"today": today_vol, "base_avg": base_avg, "threshold": ratio_threshold}
    r.fired = ratio < ratio_threshold
    return r


# ---------------------------------------------------------------------------
# 信号 4: 是否仍在涨停池 / 连板梯队 (事件型, 支撑极性)
# ---------------------------------------------------------------------------

def limit_pool_presence(db, code: str, as_of: str) -> SignalResult:
    """as_of 当日该股是否在涨停池或连板梯队内。

    支撑信号: 触发 = 动量事件仍在发生, 应压倒温和衰减 (assess_heat 编排规则)。
    诚实判别: 若两张表当日都无任何记录 → 无法区分「不在池内」与「数据没采」,
    data_ok=False (铁律 #18 反例: 空表不能当利空证据)。
    """
    r = SignalResult(name=SIG_LIMIT_POOL)
    pool_hit = bool(_rows(
        db,
        "SELECT 1 FROM stock_limit_up_down "
        "WHERE code=? AND date=? AND limit_type='limit_up' LIMIT 1",
        (code, as_of),
    ))
    ladder_hit = bool(_rows(
        db,
        "SELECT 1 FROM limit_up_ladder WHERE code=? AND trade_date=? LIMIT 1",
        (code, as_of),
    ))
    if not pool_hit and not ladder_hit:
        covered = _rows(
            db,
            "SELECT (SELECT COUNT(*) FROM stock_limit_up_down WHERE date=?) + "
            "(SELECT COUNT(*) FROM limit_up_ladder WHERE trade_date=?) AS n",
            (as_of, as_of),
        )
        if covered and covered[0]["n"] == 0:
            r.data_ok = False
            r.detail = {"reason": "both_tables_empty_for_date"}
            return r

    r.value = 1 if (pool_hit or ladder_hit) else 0
    r.detail = {"limit_up_pool": pool_hit, "ladder": ladder_hit}
    r.fired = pool_hit or ladder_hit
    return r


# ---------------------------------------------------------------------------
# 信号 5: 龙虎榜频次 (近 N 交易日上榜次数, 支撑极性)
# ---------------------------------------------------------------------------

def lhb_frequency(db, code: str, as_of: str,
                  window: int = 5, min_count: int = 1) -> SignalResult:
    """近 window 个交易日 (以 lhb_daily 自身日期为交易日历) 内上榜次数。

    支撑信号: 龙虎榜 = 市场关注度事件。lhb_daily 的 DISTINCT date 天然是交易日序列,
    直接取最近 window 个, 不依赖外部日历。
    fired 条件: count >= min_count (默认 1)。表无任何日期 → data_ok=False。
    """
    r = SignalResult(name=SIG_LHB_FREQ)
    date_rows = _rows(
        db,
        "SELECT DISTINCT date FROM lhb_daily WHERE date<=? ORDER BY date DESC LIMIT ?",
        (as_of, window),
    )
    if not date_rows:
        r.data_ok = False
        r.detail = {"reason": "lhb_no_dates"}
        return r

    dates = [row["date"] for row in date_rows]
    placeholders = ", ".join("?" for _ in dates)
    cnt_rows = _rows(
        db,
        f"SELECT COUNT(*) AS n FROM lhb_daily WHERE code=? AND date IN ({placeholders})",
        (code, *dates),
    )
    count = cnt_rows[0]["n"] if cnt_rows else 0
    r.value = count
    r.detail = {"window_dates": dates, "min_count": min_count}
    r.fired = count >= min_count
    return r


# ---------------------------------------------------------------------------
# 编排: 五路信号 → 显式规则表结论 (不是黑盒分数)
# ---------------------------------------------------------------------------

REC_HOLD = "hold"                        # 支撑信号在场 → 动量未破
REC_EXIT_REVIEW = "exit_review"          # 三路衰减齐发且无支撑
REC_LEAN_EXIT_REVIEW = "lean_exit_review"  # 两路衰减无支撑
REC_LEAN_HOLD_REVIEW = "lean_hold_review"  # 单路衰减无支撑
REC_INSUFFICIENT = "insufficient_signal"   # 无有效信号, 复评中性处理


@dataclass
class HeatAssessment:
    code: str
    as_of: str
    signals: dict[str, SignalResult] = field(default_factory=dict)
    decay_count: int = 0
    support_count: int = 0
    recommendation: str = REC_INSUFFICIENT

    def summary(self) -> str:
        """一行人读摘要, 用于 action_records.reason。"""
        parts = []
        for name, sig in self.signals.items():
            state = "n/a" if not sig.data_ok else ("ON" if sig.fired else "off")
            parts.append(f"{name}={state}({sig.value})")
        return f"[热度] {self.code}@{self.as_of} d={self.decay_count} s={self.support_count} → {self.recommendation} | " + "; ".join(parts)

    def to_dict(self) -> dict:
        """完整快照, 用于 action_records.action_detail (复评留痕交付物③的数据面)。"""
        return {
            "code": self.code,
            "as_of": self.as_of,
            "signals": {
                name: {"fired": s.fired, "value": s.value,
                       "data_ok": s.data_ok, "detail": s.detail}
                for name, s in self.signals.items()
            },
            "decay_count": self.decay_count,
            "support_count": self.support_count,
            "recommendation": self.recommendation,
        }


def assess_heat(db, code: str, as_of: str) -> HeatAssessment:
    """跑满五路信号并按显式规则给建议。

    规则 (铁律 #19 — 不同组合导向不同处置, 全部可见可测):
      任一支撑信号在场           → hold           (动量事件优先于温和衰减)
      三路衰减齐发且无支撑       → exit_review
      两路衰减无支撑             → lean_exit_review
      单路衰减无支撑             → lean_hold_review
      有效信号全无               → insufficient_signal
    单路信号异常只降级该路 (data_ok=False), 不炸整体评估。
    """
    runners = (
        lambda: sector_outflow(db, code, as_of),
        lambda: stock_outflow_streak(db, code, as_of),
        lambda: volume_decay(db, code, as_of),
        lambda: limit_pool_presence(db, code, as_of),
        lambda: lhb_frequency(db, code, as_of),
    )
    signals: dict[str, SignalResult] = {}
    for run in runners:
        try:
            sig = run()
        except Exception as exc:  # noqa: BLE001 — 单路故障不拖垮复评
            logger.warning("heat signal %s failed for %s: %s", run, code, exc)
            sig = SignalResult(name="unknown", data_ok=False,
                               detail={"reason": f"exception:{exc}"})
        signals[sig.name] = sig

    decay = sum(1 for n in DECAY_SIGNALS
                if signals.get(n) and signals[n].data_ok and signals[n].fired)
    support = sum(1 for n in SUPPORT_SIGNALS
                  if signals.get(n) and signals[n].data_ok and signals[n].fired)

    if support >= 1:
        rec = REC_HOLD
    elif decay >= 3:
        rec = REC_EXIT_REVIEW
    elif decay == 2:
        rec = REC_LEAN_EXIT_REVIEW
    elif decay == 1:
        rec = REC_LEAN_HOLD_REVIEW
    else:
        rec = REC_INSUFFICIENT

    return HeatAssessment(code=code, as_of=as_of, signals=signals,
                          decay_count=decay, support_count=support,
                          recommendation=rec)
