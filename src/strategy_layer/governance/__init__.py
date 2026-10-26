"""ST-GOV 因子治理 — 退役线 + ic_decay 刹车三件 (统筹派单 sm-25d60e49f5e046d3)

①退役线: factor_weights 权重 < 0.01 连续 RETIRE_CONSEC_DAYS 日 → 冻结计算
   + 报表标注"已退役" (不删, IC 复活可重启: 权重回 0.01+ 或显式重启)
②ic_decay 刹车三件:
   - 分层窗: 20 日快通道 + 60 日慢通道加权 (旧: 单 20 日窗追噪声)
   - 显著性闸: |IR| (ICIR) > IR_GATE 才允许调权
   - 降档观察: 全市场 IC 中位数 < MARKET_IC_FLOOR 时不普降权重,
     只打 "degraded_observation" 标记 (风格切换期的防御)
"""
from common.logging import get_logger

logger = get_logger("strategy.governance")

# ---- 治理参数 (单点定义, 测试锁) ----
RETIRE_WEIGHT_FLOOR = 0.01      # 权重死线
RETIRE_CONSEC_DAYS = 20         # 连续死线天数
IR_GATE = 0.5                   # |ICIR| 低于此值不调权 (追噪声闸)
FAST_WINDOW = 20                # 快通道窗
SLOW_WINDOW = 60                # 慢通道窗
FAST_WEIGHT = 0.4               # 快通道权重 (慢通道 0.6)
MARKET_IC_FLOOR = -0.03         # 全市场 IC 中位数低于此 → 降档观察


def compute_retired_factors(weight_rows: list[dict]) -> dict[str, dict]:
    """①退役线判定 — 真库回放入口。

    Args:
        weight_rows: [{weight_date, factor_name, weight}, ...] 升序或任意序均可
    Returns:
        {factor_name: {"retired": bool, "consec_days": int, "last_weight": float}}
        retired=True 表示连续 >= RETIRE_CONSEC_DAYS 日权重 < RETIRE_WEIGHT_FLOOR。
        IC 复活重启: 只要任一后续日 weight >= floor, consec_days 归零 (复活语义)。
    """
    by_factor: dict[str, list[tuple[str, float]]] = {}
    for r in weight_rows:
        f, w = r.get("factor_name"), r.get("weight")
        if f is None or w is None:
            continue
        by_factor.setdefault(f, []).append((str(r.get("weight_date", "")), float(w)))
    out: dict[str, dict] = {}
    for f, pairs in by_factor.items():
        pairs.sort()  # 按 (date, weight) 日期升序
        consec = 0
        for _, w in pairs:
            if w < RETIRE_WEIGHT_FLOOR:
                consec += 1
            else:
                consec = 0  # 复活: 回到线上一笔即清零
        out[f] = {
            "retired": consec >= RETIRE_CONSEC_DAYS,
            "consec_days": consec,
            "last_weight": pairs[-1][1] if pairs else None,
        }
    return out


def is_factor_frozen(retired_map: dict[str, dict], factor_name: str) -> bool:
    """计算冻结判定 — engine 消费 (retired → 跳过 compute, 省算力)。"""
    return bool(retired_map.get(factor_name, {}).get("retired"))


def layered_ic(ic_series: list[float]) -> float | None:
    """②刹车1: 分层 IC — 20 日快通道 + 60 日慢通道加权。

    快通道捕捉近期风格切换, 慢通道锚定中期趋势; 数据不足时诚实 None。
    """
    s = [x for x in ic_series if x is not None]
    if len(s) < FAST_WINDOW:
        return None
    fast = sum(s[-FAST_WINDOW:]) / FAST_WINDOW
    slow_part = s[-SLOW_WINDOW:]
    if len(slow_part) < SLOW_WINDOW:
        return fast  # 慢通道数据未满 → 只用快通道 (诚实降级, 不造假)
    slow = sum(slow_part) / SLOW_WINDOW
    return FAST_WEIGHT * fast + (1 - FAST_WEIGHT) * slow


def significance_gate(icir: float | None) -> bool:
    """②刹车2: 调权显著性闸 — |IR| > 0.5 才允许调权。"""
    if icir is None:
        return False
    return abs(icir) > IR_GATE


def market_regime_flag(factor_ics: dict[str, float]) -> str | None:
    """②刹车3: 全市场 IC 中位数大负 → "degraded_observation" 标记 (不普降权重)。"""
    vals = [v for v in factor_ics.values() if v is not None]
    if not vals:
        return None
    vals_sorted = sorted(vals)
    n = len(vals_sorted)
    med = vals_sorted[n // 2] if n % 2 else (vals_sorted[n // 2 - 1] + vals_sorted[n // 2]) / 2
    if med < MARKET_IC_FLOOR:
        return "degraded_observation"
    return None


def apply_governance(current_weights: dict[str, float],
                     ic_history: list[dict],
                     factor_ics_today: dict[str, float] | None = None,
                     ) -> dict[str, object]:
    """ic_decay 调权治理入口 — 三刹车全装。

    Args:
        current_weights: 当前权重表
        ic_history: [{factor, ic, icir?}, ...] 时序 (任意窗内)
        factor_ics_today: 当日全市场因子 IC {factor: ic} (供降档观察)
    Returns:
        {"weights": dict, "frozen": set, "regime_flag": str|None, "gate_skipped": set}
    """
    regime = market_regime_flag(factor_ics_today or {})
    if regime:
        logger.warning("ST-GOV: 全市场 IC 中位数大负, 触发降档观察 (不普降权重)")
    # 刹车3: 降档观察期 → 冻结一切调权动作, 只打标
    if regime == "degraded_observation":
        return {"weights": dict(current_weights), "frozen": set(),
                "regime_flag": regime, "gate_skipped": set()}

    by_factor: dict[str, list[dict]] = {}
    for e in ic_history:
        f = e.get("factor")
        if f:
            by_factor.setdefault(f, []).append(e)

    weights = dict(current_weights)
    gate_skipped: set[str] = set()
    for f, entries in by_factor.items():
        if f not in weights:
            continue
        icir = entries[-1].get("icir")
        if not significance_gate(icir):
            gate_skipped.add(f)  # 刹车2: IR 不显著 → 跳过调权
            continue
        ic_series = [e.get("ic") for e in entries if e.get("ic") is not None]
        li = layered_ic(ic_series)  # 刹车1: 分层窗
        if li is None:
            continue
        if li < -0.05:
            weights[f] = weights[f] * 0.7
        elif li > 0.05:
            weights[f] = min(weights[f] * 1.1, 0.5)
    total = sum(weights.values()) or 1.0
    return {"weights": {k: round(v / total, 6) for k, v in weights.items()},
            "frozen": set(), "regime_flag": None, "gate_skipped": gate_skipped}
