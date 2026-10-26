"""Debate 数据模型"""
from dataclasses import dataclass, field


@dataclass
class ExpertReport:
    expert_name: str
    score: float
    key_points: list[str] = field(default_factory=list)
    confidence: float = 0.5
    # AG-COMPOSE: 判读标注 — 规则腿 "rules" / LLM 腿 "llm" (防假绿: 回执必须能分辨)
    tier_used: str = "rules"
    # AG-DEBATE-V2 ②: 条件失效声明 — "什么情况下这个判断作废"
    # (跌破某价位/量能萎缩/公告反转), 格式对齐执行层条件单 (EX-4 下游)。
    invalidation_conditions: list[str] = field(default_factory=list)


@dataclass
class DebateRound:
    round_num: int
    speaker: str
    content: str
    rebuttal_to: str | None = None


@dataclass
class DebateState:
    stock_code: str
    round: int = 0
    expert_reports: list[ExpertReport] = field(default_factory=list)
    bull_args: list[str] = field(default_factory=list)
    bear_args: list[str] = field(default_factory=list)
    debate_history: list[DebateRound] = field(default_factory=list)
    fulcrum_analysis: str | None = None
    consensus: str | None = None
    confidence: float = 0.0
    divergences: list = field(default_factory=list)


@dataclass
class FinalDecision:
    verdict: str
    confidence: float
    position_pct: float
    reasoning: str
    risk_flags: list[str] = field(default_factory=list)
    requires_human: bool = False
    # REC 模块1a (2026-09-21): 结构化论证锚 — 决策产出时由 rationale 层
    # 落库后回填, 接通 stock_pool.added_reason (模块1 接通面, 替换排名话术)。
    rationale_id: str = ""
