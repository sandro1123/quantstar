"""因子相关性分析"""

from dataclasses import dataclass, field

import numpy as np
from scipy.stats import spearmanr


@dataclass
class CorrelationPair:
    factor_a: str
    factor_b: str
    correlation: float
    is_redundant: bool = False


@dataclass
class CorrelationReport:
    pairs: list[CorrelationPair] = field(default_factory=list)
    redundant_pairs: list[CorrelationPair] = field(default_factory=list)

    @property
    def redundancy_ratio(self) -> float:
        if not self.pairs:
            return 0.0
        return len(self.redundant_pairs) / len(self.pairs)


class FactorCorrelationAnalyzer:
    """Factor correlation analysis"""

    def __init__(self, threshold: float = 0.7):
        self.threshold = threshold

    def analyze(self, factor_matrix: dict[str, dict[str, float]]) -> CorrelationReport:
        """
        factor_matrix: {factor_name: {code: value}}
        Returns: CorrelationReport with all pairs
        """
        factors = list(factor_matrix.keys())
        report = CorrelationReport()

        if len(factors) < 2:
            return report

        for i in range(len(factors)):
            for j in range(i + 1, len(factors)):
                fa, fb = factors[i], factors[j]
                values_a = np.array(list(factor_matrix[fa].values()))
                values_b = np.array(list(factor_matrix[fb].values()))

                mask = ~(np.isnan(values_a) | np.isnan(values_b))
                if mask.sum() < 10:
                    continue

                corr, _ = spearmanr(values_a[mask], values_b[mask])
                pair = CorrelationPair(
                    factor_a=fa,
                    factor_b=fb,
                    correlation=float(corr),
                    is_redundant=abs(corr) > self.threshold,
                )
                report.pairs.append(pair)
                if pair.is_redundant:
                    report.redundant_pairs.append(pair)

        return report

    def get_weight_penalties(self, report: CorrelationReport) -> dict[str, float]:
        """Calculate weight penalties based on redundancy"""
        penalties: dict[str, float] = {}
        for pair in report.redundant_pairs:
            for f in [pair.factor_a, pair.factor_b]:
                penalties[f] = penalties.get(f, 0.0) + 0.1
        return penalties
