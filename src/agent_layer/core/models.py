"""Agent Layer 数据模型 — Pydantic BaseModel"""
from typing import Any, Optional

from pydantic import BaseModel, Field

from common.utils import generate_trace_id


class AgentRequest(BaseModel):
    task: str
    input: dict = Field(default_factory=dict)
    consumer: str = "unknown"
    format: str = "structured"
    depth: str = "normal"
    context: dict | None = None
    trace_id: str = Field(default_factory=generate_trace_id)
    sanitize_input: bool = True
    version: str = "1.1"
    # AG-COMPOSE 三新键 (2026-09-07 请求规范, 用户令):
    # style — 回复风格 (professional_advisor 专业投顾/researcher 研报/risk_officer 风控)
    # max_words — 回复字数上限 (专家模板 max_words 位)
    # focus — 请求方关注点列表 (穿专家模板 focus 位)
    style: str = "professional_advisor"
    max_words: int = 200
    focus: list[str] = Field(default_factory=list)


class AgentState(BaseModel):
    request: AgentRequest
    responses: list = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    start_time: float = 0.0
    version: int = 1
    requires_human: bool = False
    human_decision: str | None = None

    def add_response(self, response: 'AgentResponse'):
        self.responses.append(response)
        self.version += 1

    def add_error(self, error: str):
        self.errors.append(error)

    @property
    def latest_response(self) -> Optional['AgentResponse']:
        return self.responses[-1] if self.responses else None


class FundamentalScore(BaseModel):
    pe: float = 0.0
    pb: float = 0.0
    roe: float = 0.0
    revenue_growth: float = 0.0


class TechnicalScore(BaseModel):
    trend: str = "neutral"
    support: float = 0.0
    resistance: float = 0.0
    momentum: float = 0.0


class SentimentScore(BaseModel):
    turnover_ratio: float = 0.0
    volume_change: float = 0.0


class RiskFlag(BaseModel):
    flag: str
    severity: str = "medium"


class QualityMetrics(BaseModel):
    model_used: str = ""
    tier_used: str = "full"
    latency_ms: int = 0
    providers_tried: list = Field(default_factory=list)
    was_cached: bool = False
    degradation_reason: str | None = None
    total_tokens: int = 0
    cost: float = 0.0


class AgentResponse(BaseModel):
    request_id: str = ""
    task: str = ""
    code: str = ""
    output: Any = None
    verdict: str = "观望"
    confidence: float = 0.5
    reasoning: str = ""
    fundamental: FundamentalScore | None = None
    technical: TechnicalScore | None = None
    sentiment: SentimentScore | None = None
    risk_flags: list[RiskFlag] = Field(default_factory=list)
    divergences: list = Field(default_factory=list)
    agents_used: list[str] = Field(default_factory=list)
    quality: QualityMetrics = Field(default_factory=QualityMetrics)
    output_validated: bool = False
    validation_warnings: list[str] = Field(default_factory=list)
    version: str = "1.0"
    degraded_fallback: bool = False
