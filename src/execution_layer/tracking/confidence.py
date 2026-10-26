# -*- coding: utf-8 -*-
"""execution_layer/tracking/confidence.py — 信心分档执行层薄封装 (REC 通电卡, 2026-09-24)。

统筹裁定 ③: 执行层薄封装转调 backtest_layer.evaluation.agent_eval.confidence_tier,
**不新建 execution→backtest 的跨层 import 边**(十层契约条款3: 禁跨层副作用调用;
薄封装转调是登记契约内的只读复用)。

分档语义权威定义点在 agent_eval.confidence_tier (high≥0.6/mid 0.4~0.6/low<0.4/
None→unknown, 阈值走 TIER_THRESHOLDS 常量)。本模块**不重复定义阈值** —
每次转调权威函数, 保证单一定义点。
"""
from __future__ import annotations


def confidence_tier_for_tracking(confidence: float | None) -> str:
    """信心分档 (转调权威实现, 不重写阈值)。

    Args:
        confidence: 置信度 0~1, 或 None (缺失)

    Returns:
        "high" / "mid" / "low" / "unknown"
    """
    from backtest_layer.evaluation.agent_eval import confidence_tier
    return confidence_tier(confidence)


def confidence_reason_for_tracking(confidence: float | None) -> str:
    """信心档位的人话原因 (讨论#3 定谳: 信心可低不可假装高)。

    红线: 缺失/低信心如实陈述, 不粉饰。
    """
    tier = confidence_tier_for_tracking(confidence)
    if tier == "unknown":
        return "信心未标注 (置信度缺失, 不假装有把握)"
    if tier == "low":
        return f"低信心 (置信度 {confidence:.2f} < 0.4): 依据较弱, 仅供观察"
    if tier == "mid":
        return f"中信心 (置信度 {confidence:.2f}): 依据成立但存在不确定因素"
    return f"高信心 (置信度 {confidence:.2f}): 依据充分"
