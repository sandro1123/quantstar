"""跨引擎持仓相关性监控"""

from dataclasses import dataclass, field


@dataclass
class EngineHolding:
    engine_name: str
    codes: list[str]
    weight: float = 1.0


@dataclass
class CorrelationPair:
    engine_a: str
    engine_b: str
    jaccard_similarity: float
    overlap_count: int
    total_unique: int
    is_highly_correlated: bool = False


@dataclass
class CorrelationReport:
    pairs: list[CorrelationPair] = field(default_factory=list)
    highly_correlated_pairs: list[CorrelationPair] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class EngineCorrelationMonitor:
    """监控多引擎持仓相关性"""

    def __init__(self, threshold: float = 0.7):
        self.threshold = threshold

    def analyze(self, engines: list[EngineHolding]) -> CorrelationReport:
        """分析所有引擎的持仓相关性"""
        report = CorrelationReport()

        if len(engines) < 2:
            return report

        for i in range(len(engines)):
            for j in range(i + 1, len(engines)):
                ea, eb = engines[i], engines[j]
                set_a = set(ea.codes)
                set_b = set(eb.codes)

                intersection = set_a & set_b
                union = set_a | set_b

                if not union:
                    continue

                jaccard = len(intersection) / len(union)
                pair = CorrelationPair(
                    engine_a=ea.engine_name,
                    engine_b=eb.engine_name,
                    jaccard_similarity=round(jaccard, 4),
                    overlap_count=len(intersection),
                    total_unique=len(union),
                    is_highly_correlated=jaccard > self.threshold,
                )
                report.pairs.append(pair)

                if pair.is_highly_correlated:
                    report.highly_correlated_pairs.append(pair)
                    report.warnings.append(
                        f"高相关性告警: {ea.engine_name} 与 {eb.engine_name} "
                        f"持仓相似度 {jaccard:.1%}（交集 {len(intersection)} 只）"
                    )

        return report

    def get_diversification_score(self, engines: list[EngineHolding]) -> float:
        """计算分散度评分 (0~1, 越高越分散)"""
        report = self.analyze(engines)
        if not report.pairs:
            return 1.0
        avg_similarity = sum(p.jaccard_similarity for p in report.pairs) / len(report.pairs)
        return round(1.0 - avg_similarity, 4)
