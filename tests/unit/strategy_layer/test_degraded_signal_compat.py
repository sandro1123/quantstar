# -*- coding: utf-8 -*-
"""D1 兼容层: 「LLM 是否降级」判据 (2026-10-21)。

背景: 用户视角席做 D1 (六 section 积累中话术三分支化), 需要读「LLM 降级」
信号, 但 `degraded_fallback` 字段只在 F-21 分支 (1fbb91ab) 上, main 没有。

口径 (统筹裁定 sm-a20c9e1cfa53414d):
  ① 有 degraded_fallback 属性 -> 直接读它 (最权威, F-21 并卡后自动生效)
  ② 没有该属性 -> 只在能确认是 LLM 调用侧时才算降级:
     quality.tier_used == "degraded" 且 degradation_reason 命中 LLM 侧特征词
     (410/429/timeout/超时/未调LLM/all providers failed/circuit open);
     degradation_reason 为空或指向守卫/配置 -> 不算 LLM 降级
  ③ 都不成立 -> 返回 None (不明), **绝不返回 False**

**为何 ② 必须收窄** (铁律#12 前提须写进注释):
  tier_used=="degraded" 只说「降级发生了」, 不区分来源。至少混三类:
    - LLM 全挂/429/410/超时 -> _fallback_response 兜底 0.30  => 是 LLM 降级
    - 守卫拦截 (risk veto/三闸/置信度不足) => 业务判断, 不是 LLM 降级
    - 系统关闭/未启用 (agent_gate disabled) => 配置, 不是 LLM 降级
  把后两类算成「LLM 降级」= 明明策略判断「这票不该买」却告诉用户
  「模型服务异常」-> 用户以为系统坏了, 去查一个不存在的故障 = 造新假象。

**None 的含义**: 「不明」≠「健康」。调用方遇 None 必须走「原因不明」分支,
不许当健康处理 (否则就是下一个「积累中」)。
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from strategy_layer.agent_gate.degraded_signal import (  # noqa: E402
    llm_degraded_state,
)


class _Quality:
    def __init__(self, tier_used="full", degradation_reason=""):
        self.tier_used = tier_used
        self.degradation_reason = degradation_reason


class _Resp:
    """模拟 AgentResponse (LLM 侧对象, main 已有 quality 字段)。"""

    def __init__(self, tier_used="full", degradation_reason=""):
        self.quality = _Quality(tier_used, degradation_reason)
        self.reasoning = ""


class _ReviewWithFlag:
    """模拟 F-21 的 AgentReviewResult (有 degraded_fallback 属性)。"""

    def __init__(self, degraded_fallback=True):
        self.degraded_fallback = degraded_fallback
        self.quality = _Quality("degraded", "410 熔断")
        self.reasoning = ""


class _ReviewNoFlag:
    """main 的 AgentReviewResult (无 degraded_fallback 属性, 无 quality)。"""

    def __init__(self, reasoning="正常结论"):
        self.reasoning = reasoning
        self.confidence = 0.8


def test_dedicated_degraded_fallback_flag_is_read():
    """钉(1) 有 degraded_fallback 属性 -> 直接读它 (最权威)。"""
    assert llm_degraded_state(_ReviewWithFlag(True)) is True
    assert llm_degraded_state(_ReviewWithFlag(False)) is False


def test_falls_back_to_quality_signals_without_crashing():
    """钉(2) 没有该属性 -> 回退到 quality 观测面, 不崩。"""
    # LLM 侧降级特征词 -> True
    for reason in ("410 熔断", "429 限流", "timeout", "超时降级 (deep > 120s)",
                   "未调LLM", "all providers failed", "circuit open"):
        st = llm_degraded_state(_Resp("degraded", reason))
        assert st is True, f"LLM 侧降级特征应判 True: {reason!r} -> {st}"
    # 非 degraded tier -> 非 LLM 降级
    assert llm_degraded_state(_Resp("full", "")) is False
    assert llm_degraded_state(_Resp("entry_rule", "")) is False


def test_unknown_returns_none_never_false():
    """钉(3) 都拿不到 -> 返回 None, **绝不返回 False** (灵魂钉)。"""
    st = llm_degraded_state(_ReviewNoFlag())
    assert st is None, f"不明必须返回 None, 实得 {st!r} (False 会把不明当健康)"
    assert st is not False
    # 无 quality 的 degraded 档但 reason 指向非 LLM -> 同样不明
    st2 = llm_degraded_state(_Resp("degraded", ""))
    assert st2 is None, f"空 reason 不可推断 LLM 降级, 实得 {st2!r}"


def test_guardrail_rejection_not_mistaken_for_llm_degradation():
    """钉(4) 守卫拦截不得被误判为 LLM 降级 (业务判断 != 系统故障)。"""
    for reason in ("risk veto", "置信度不足", "Guardrail: 违规", "系统正在关闭, 已降级"):
        st = llm_degraded_state(_Resp("degraded", reason))
        assert st is not True, (
            f"业务/配置类降级不得判 LLM 降级: {reason!r} -> {st!r}")
