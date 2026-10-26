"""Agent Layer — 跨层返回类型"""
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentResult:
    verdict: str = "观望"
    confidence: float = 0.5
    reasoning: str = ""
    output: Any = None
    risk_flags: list = field(default_factory=list)
    # AG-11: 质量元数据 (model_used/tier_used/latency_ms/providers_tried); AG-03 将纳入 OutputSpec 契约
    quality: dict = field(default_factory=dict)


@dataclass
class AccuracyReport:
    total: int = 0
    hits: int = 0
    accuracy: float = 0.0


@dataclass
class ProviderHealthReport:
    providers_online: int = 0
    providers_total: int = 0
    degradation_active: bool = False
    degradation_tier: str = ""
    degradation_reason: str = ""
    pool_health: dict = field(default_factory=dict)


@dataclass
class PredictionRecord:
    request_id: str = ""
    agent_name: str = ""
    code: str = ""
    date: str = ""
    verdict: str = ""
    confidence: float = 0.0
    hit: int | None = None
    created_at: str = ""
