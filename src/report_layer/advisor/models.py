# -*- coding: utf-8 -*-
"""推荐逻辑贯通卡 模块1b — 报告层二次加工 数据模型

原料对齐 agent_layer/debate/models.py 现状形状（FinalDecision/ExpertReport）。
W1 的 recommendation_rationale schema 出来后，只换字段映射，模型结构不变。

追溯链设计（统筹加固要求2）：AdvisorSentence.trace_id 可与 W1 rationale 表同库 JOIN：
用户看到的每句话 → (sentence.trace_id) → 加工产出 → (rationale_id) → 原始论证 → LLM run_id
rationale_id 先留 None 位（W1 schema 落表后接通），保证不落成孤岛。
"""
from dataclasses import dataclass, field
from typing import Optional

# 闸3 source 白名单 — 只有三来源，多一个都拒
SOURCE_WHITELIST = ("reasoning", "key_points", "invalidation_conditions")


@dataclass
class RecommendationArgument:
    """内部论证原料（1a schema 落地前的现实形状，字段名对齐 debate/models.py）"""
    code: str
    verdict: str                    # buy / hold / avoid（FinalDecision.verdict）
    confidence: float               # 0~1（FinalDecision.confidence）
    position_pct: float             # FinalDecision.position_pct
    reasoning: str                  # 裁决官终审叙述
    key_points: list = field(default_factory=list)      # ExpertReport.key_points 汇总
    invalidation_conditions: list = field(default_factory=list)  # 条件失效声明
    factors: list = field(default_factory=list)         # 原料中出现的因子名
    numbers: list = field(default_factory=list)         # 原料中出现的数字（含单位）
    claims: list = field(default_factory=list)          # 1a claims JSON（合法扩展域：每句 {text, source{kind,ref,value}}）
    rationale_id: Optional[str] = None  # W1 rationale 表主键（fe45b0f3 契约，JOIN 键）
    trace_id: Optional[str] = None      # agent_checkpoints.trace_id（三锚之一，审计回 JOIN）


@dataclass
class AdvisorSentence:
    """二次加工产出的一句，source 必须在白名单内（闸3）"""
    text: str
    source: Optional[str] = None
    trace_id: Optional[str] = None  # 落库 JOIN 键（闸3可追溯）


@dataclass
class AdvisorResult:
    """加工结果（放行或降级）"""
    sentences: list = field(default_factory=list)
    degraded: bool = False          # True=守卫拒绝后降级（原始reasoning+术语小注）
    text: str = ""
    rationale_id: Optional[str] = None
