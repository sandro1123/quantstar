"""信号生成 — 生成 buy/sell/hold 信号"""
import math
from dataclasses import dataclass, field
from uuid import uuid4

from common.logging import get_logger
from strategy_layer.risk.protections import CooldownPeriod
from strategy_layer.selection.selector import SelectionResult

logger = get_logger("strategy.signals")


@dataclass
class TradeSignal:
    signal_id: str = field(default_factory=lambda: str(uuid4())[:8])
    code: str = ""
    date: str = ""
    action: str = ""           # "buy" | "sell" | "hold"
    direction: str = ""        # "long" | "short" | "close"
    magnitude: float = 0.0     # 0.0 ~ 1.0, how strong
    strength: float = 0.0      # 0.0 ~ 1.0 (kept for backward compat)
    confidence: float = 0.0    # 0.0 ~ 1.0
    ttl: int = 1               # days until signal expires
    source_model: str = ""     # which model/strategy generated it
    reason: str = ""
    entry_low: float = 0.0
    entry_high: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    expected_cost: float = 0.0  # 预期交易成本 (百分比)


class SignalGenerator:
    def __init__(self, config=None):
        self.config = config or {}
        cost_cfg = self.config.get("strategy.trading.cost", {"buy": 0.0003, "sell": 0.0013})
        self._buy_cost = cost_cfg.get("buy", 0.0003)
        self._sell_cost = cost_cfg.get("sell", 0.0013)
        atr_cfg = self.config.get("strategy.position.atr_multiplier", {"entry": 0.5, "stop": 2, "profit": 3})
        self._atr_entry = atr_cfg.get("entry", 0.5)
        self._atr_stop = atr_cfg.get("stop", 2)
        self._atr_profit = atr_cfg.get("profit", 3)
    def generate(self, selection: SelectionResult, current_positions: dict | None = None,
                 cooldown: CooldownPeriod | None = None,
                 stock_prices: dict[str, float] | None = None,
                 atr_values: dict[str, float] | None = None) -> list[TradeSignal]:
        signals = []
        current = current_positions or {}
        prices = stock_prices or {}
        atrs = atr_values or {}

        if not selection.candidates:
            return signals

        top_score = selection.candidates[0].composite_score if selection.candidates else 0
        bottom_score = selection.candidates[-1].composite_score if selection.candidates else 0
        score_range = top_score - bottom_score if top_score != bottom_score else 1.0

        # ST-46: 用 enumerate 提供候选池内序位 — candidate.rank 是 aggregate() 在
        # ML blend/L3.3 调整前的全市场纯因子排名且此后不更新, 直接写进 reason 会误导
        # (实测 688334 记录 "排名 #1" 而 blend 后真实序位是第 2 位)。
        for i, candidate in enumerate(selection.candidates):
            strength = max(0.0, min(1.0, (candidate.composite_score - bottom_score) / score_range))
            if candidate.code not in current:
                if cooldown:
                    result = cooldown.check(candidate.code, selection.date)
                    if not result.can_trade:
                        logger.info(f"Skipping {candidate.code}: {result.reason}")
                        continue
                confidence = round(1 / (1 + math.exp(-candidate.composite_score * 2)), 3)
                # ST-12: 原此处有孤儿表达式 (candidate.composite_score / abs(top_score)) * strength,
                # 计算结果未赋值给任何变量 — confidence 重构残留, 已删除 (审计 S-19)。
                expected_cost = self._buy_cost + self._sell_cost

                entry_low = 0.0
                entry_high = 0.0
                stop_loss = 0.0
                take_profit = 0.0
                price = prices.get(candidate.code, 0.0)
                atr = atrs.get(candidate.code, 0.0)
                # atr 未提供时使用价格 2% 作为 fallback (近似日均波动)
                if price > 0 and atr <= 0:
                    atr = price * 0.02
                if price > 0 and atr > 0:
                    entry_low = round(price - self._atr_entry * atr, 2)
                    entry_high = round(price + self._atr_entry * atr, 2)
                    stop_loss = round(price - self._atr_stop * atr, 2)
                    take_profit = round(price + self._atr_profit * atr, 2)

                signals.append(TradeSignal(
                    code=candidate.code, date=selection.date,
                    action="buy", direction="long",
                    magnitude=round(strength, 3),
                    strength=round(strength, 3),
                    confidence=confidence,
                    ttl=1, source_model="factor_engine",
                    reason=(f"候选池第 {i + 1}/{len(selection.candidates)} 位, "
                            f"综合得分 {candidate.composite_score:.3f}"),
                    expected_cost=round(expected_cost * 100, 4),
                    entry_low=entry_low, entry_high=entry_high,
                    stop_loss=stop_loss, take_profit=take_profit,
                ))
            else:
                signals.append(TradeSignal(
                    code=candidate.code, date=selection.date,
                    action="hold", direction="long",
                    magnitude=round(strength, 3),
                    strength=round(strength, 3),
                    confidence=0.8, ttl=1, source_model="factor_engine",
                    reason=(f"已有持仓, 候选池第 {i + 1}/{len(selection.candidates)} 位, "
                            f"综合得分 {candidate.composite_score:.3f}"),
                ))

        # 为现有持仓生成 sell/hold 信号
        for code, pos in current.items():
            in_candidates = any(c.code == code for c in selection.candidates)
            if not in_candidates:
                # 已不在候选名单中 → sell
                signals.append(TradeSignal(
                    code=code, date=selection.date,
                    action="sell", direction="close",
                    magnitude=0.8, strength=0.8,
                    confidence=0.7, ttl=1, source_model="factor_engine",
                    reason="评分滑出候选名单",
                ))

        logger.info(f"Generated {len(signals)} signals: "
                     f"{sum(1 for s in signals if s.action == 'buy')} buy, "
                     f"{sum(1 for s in signals if s.action == 'hold')} hold, "
                     f"{sum(1 for s in signals if s.action == 'sell')} sell")
        return signals
