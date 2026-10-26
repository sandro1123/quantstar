"""agent_layer/committee/chairman.py — Chairman collecting all 7 analyst opinions"""

from dataclasses import dataclass, field

from agent_layer.committee.analyst_value import (
    FundamentalAnalyst,
    MomentumAnalyst,
    QuantAnalyst,
    RiskAnalyst,
    SentimentAnalyst,
    TechnicalAnalyst,
    ValueAnalyst,
)
from common.logging import get_logger

logger = get_logger("agent.committee")

ALL_ANALYSTS = [
    ValueAnalyst, MomentumAnalyst, RiskAnalyst,
    TechnicalAnalyst, FundamentalAnalyst, SentimentAnalyst, QuantAnalyst,
]


@dataclass
class CommitteeResult:
    verdict: str = "观望"
    score: float = 0.0
    confidence: float = 0.5
    opinions: list = field(default_factory=list)
    reasoning: str = ""


class Chairman:
    def __init__(self):
        self.analysts = [cls() for cls in ALL_ANALYSTS]
        self.weights = {
            "value": 0.2, "momentum": 0.15, "risk": 0.2,
            "technical": 0.1, "fundamental": 0.15, "sentiment": 0.1, "quant": 0.1,
        }

    def analyze(self, code: str, factor_scores: dict) -> CommitteeResult:
        opinions = [a.analyze(code, factor_scores) for a in self.analysts]
        total_score = 0.0
        total_weight = 0.0
        for opinion in opinions:
            w = self.weights.get(opinion["analyst"], 0.1)
            total_score += opinion["score"] * w * opinion["confidence"]
            total_weight += w * opinion["confidence"]
        final_score = total_score / total_weight if total_weight > 0 else 0
        final_confidence = min(total_weight / sum(self.weights.values()), 1.0)
        if final_score > 0.25:
            verdict = "推荐"
        elif final_score < -0.25:
            verdict = "不推荐"
        else:
            verdict = "观望"
        return CommitteeResult(
            verdict=verdict, score=round(final_score, 3),
            confidence=round(final_confidence, 3),
            opinions=opinions,
            reasoning=f"{len(opinions)}位分析师综合评分 {final_score:.3f}",
        )
