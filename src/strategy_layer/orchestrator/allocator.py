"""Allocator — 跨策略资金分配 + 多目标优化"""
import math
from dataclasses import dataclass, field

from common.logging import get_logger

logger = get_logger("strategy.allocator")


@dataclass
class StrategyAllocation:
    strategy_name: str
    weight: float
    capital: float


class Allocator:
    def __init__(self, total_capital: float = 1_000_000.0):
        self.total_capital = total_capital

    def softmax_allocate(self, strategy_scores: dict[str, float], temperature: float = 1.0) -> list[StrategyAllocation]:
        if not strategy_scores:
            return []
        scores = list(strategy_scores.values())
        exp_scores = [math.exp(s / temperature) for s in scores]
        total_exp = sum(exp_scores)
        allocations = []
        for name, exp_s in zip(strategy_scores.keys(), exp_scores):
            weight = exp_s / total_exp
            allocations.append(StrategyAllocation(strategy_name=name, weight=weight, capital=self.total_capital * weight))
        logger.info(f"Allocator: {len(allocations)} strategies, softmax temp={temperature}")
        return allocations

    def equal_allocate(self, strategy_names: list[str]) -> list[StrategyAllocation]:
        if not strategy_names:
            return []
        weight = 1.0 / len(strategy_names)
        return [StrategyAllocation(name, weight, self.total_capital * weight) for name in strategy_names]

    def allocate_with_constraints(self, strategy_scores: dict[str, float], min_weight: float = 0.05, max_weight: float = 0.5) -> list[StrategyAllocation]:
        raw = self.softmax_allocate(strategy_scores)
        for a in raw:
            a.weight = max(min_weight, min(max_weight, a.weight))
            a.capital = self.total_capital * a.weight
        total = sum(a.weight for a in raw)
        for a in raw:
            a.weight /= total
            a.capital = self.total_capital * a.weight
        return raw


@dataclass
class OptimizationObjective:
    sharpe_weight: float = 0.4
    calmar_weight: float = 0.3
    win_rate_weight: float = 0.3


@dataclass
class MultiObjectiveResult:
    sharpe: float = 0.0
    calmar: float = 0.0
    win_rate: float = 0.0
    composite_score: float = 0.0
    weights: dict[str, float] = field(default_factory=dict)


class MultiObjectiveOptimizer:
    """Multi-objective parameter optimization"""

    def __init__(self, objectives: OptimizationObjective | None = None):
        self.objectives = objectives or OptimizationObjective()

    def compute(self, returns: list[float], win_rates: list[float], weights: dict[str, float]) -> MultiObjectiveResult:
        import numpy as np

        arr = np.array(returns)

        sharpe = float(np.mean(arr) / np.std(arr) * np.sqrt(252)) if np.std(arr) > 1e-12 else 0.0

        ann_return = float(np.mean(arr) * 252)
        cummax = np.maximum.accumulate(arr)
        drawdown = (cummax - arr) / cummax
        max_dd = float(np.max(drawdown)) if len(drawdown) > 0 else 1.0
        calmar = ann_return / max_dd if max_dd > 0 else 0.0

        win_rate = float(np.mean(win_rates)) if win_rates else 0.0

        composite = (
            self.objectives.sharpe_weight * max(0, sharpe) +
            self.objectives.calmar_weight * max(0, calmar) +
            self.objectives.win_rate_weight * win_rate
        )

        return MultiObjectiveResult(
            sharpe=sharpe,
            calmar=calmar,
            win_rate=win_rate,
            composite_score=composite,
            weights=weights,
        )

    def optimize_weights(self, factor_ic_history: dict[str, list[float]]) -> dict[str, float]:
        import numpy as np

        scores = {}
        for factor, ics in factor_ic_history.items():
            arr = np.array(ics)
            if len(arr) < 5:
                scores[factor] = 0.0
                continue
            mean_ic = float(np.mean(arr))
            std_ic = float(np.std(arr))
            icir = mean_ic / std_ic if std_ic > 0 else 0.0
            scores[factor] = max(0, icir * 0.6 + mean_ic * 0.4)

        total = sum(scores.values())
        if total > 0:
            return {f: s / total for f, s in scores.items()}
        return {f: 1.0 / len(scores) for f in scores}
