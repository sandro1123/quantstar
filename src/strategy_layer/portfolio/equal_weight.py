"""EqualWeightPortfolio — 等权分配"""
from dataclasses import dataclass

from common.logging import get_logger

logger = get_logger("strategy.portfolio")


@dataclass
class PortfolioTarget:
    code: str
    weight: float
    shares: int = 0
    estimated_value: float = 0.0


class EqualWeightPortfolio:
    def __init__(self, total_capital: float = 1_000_000.0, max_positions: int = 10):
        self.total_capital = total_capital
        self.max_positions = max_positions

    def allocate(self, selected_codes: list[str], prices: dict[str, float] | None = None) -> list[PortfolioTarget]:
        n = min(len(selected_codes), self.max_positions)
        if n == 0:
            return []
        weight_per = 1.0 / n
        targets = []
        prices = prices or {}
        for code in selected_codes[:n]:
            value_per = self.total_capital * weight_per
            price = prices.get(code, 0)
            shares = int(value_per / price) if price > 0 else 0
            targets.append(PortfolioTarget(code=code, weight=weight_per, shares=shares, estimated_value=value_per))
        logger.info(f"EqualWeightPortfolio: {n} positions, {weight_per:.2%} each")
        return targets

    def rebalance(self, current_holdings: dict[str, float], new_selected: list[str], prices: dict[str, float]) -> dict[str, float]:
        targets = self.allocate(new_selected, prices)
        target_map = {t.code: t.weight for t in targets}
        total_value = sum(current_holdings.values())
        adjustments = {}
        for code, target_weight in target_map.items():
            target_value = total_value * target_weight
            current_value = current_holdings.get(code, 0)
            diff = target_value - current_value
            if abs(diff) > total_value * 0.01:
                adjustments[code] = diff
        logger.info(f"Rebalance: {len(adjustments)} adjustments needed")
        return adjustments
