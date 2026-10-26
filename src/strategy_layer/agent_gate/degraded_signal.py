# -*- coding: utf-8 -*-
"""「LLM 是否降级」判据兼容层 (2026-10-21, D1 用户视角席依赖)。

**为什么需要这一层**: 用户视角席做 D1 (六 section「积累中」话术三分支化) 时
要读「本次是不是 LLM 侧降级」, 但 `degraded_fallback` 字段只在 F-21 分支
(1fbb91ab) 上, main (ecdc4134) 没有 => D1 分支基于 main 读不到。本层做
**双源兼容**: 有就用最权威的, 没有就回退到现有可观测面, 再没有就明说「不明」。

**三态语义 (灵魂, 不可改)**:
    True  = 确认是 LLM 侧降级 (模型服务异常)
    False = 确认**不是** LLM 侧降级 (有正面证据表明这与 LLM 无关)
    None  = **不明** (既无正面证据也无反面证据)

**None 绝不等于 False**: 「不明」被当成「健康」, 就是下一个「积累中」假象
—— 用户视角席会把「原因不明」显示成「一切正常」, 掩盖真实故障。
调用方遇 None **必须**走「原因不明」分支。

**② 为何必须收窄 (铁律#12: 修复前提须写进注释)**:
    `quality.tier_used == "degraded"` 只说「降级发生了」, **不区分来源**。
    main 的 `_fallback_response` 至少混着三类降级:
        1. LLM 全挂/429/410/超时 -> 兜底 confidence=0.30  => **是** LLM 降级
        2. 守卫拦截 (risk veto/三闸/置信度不足) => **业务判断**, 不是 LLM 降级
        3. 系统关闭/未启用 (agent_gate disabled) => **配置**, 不是 LLM 降级
    把后两类算成「LLM 降级」= 明明策略判断「这票不该买」, 却告诉用户
    「模型服务异常」-> 用户以为系统坏了, 去查一个不存在的故障 = **造新假象**。
    故 ② 要求 `degradation_reason` 命中 LLM 侧特征词才判 True。

**与 F-21 的关系**: F-21 (1fbb91ab) 一旦并入 main, ①自动生效, 本层的 ②③
成为历史兼容路径, 不会与 F-21 冲突 (①优先)。

**判据面零副作用**: 本模块只读, 不写库, 不改任何既有对象。
"""

__all__ = ["llm_degraded_state", "LLM_DEGRADED_SIGNATURES"]

# LLM 调用侧降级的特征词 (小写匹配)。
# 来源: agent_layer._fallback_response 的 reason (超时降级/执行失败/Guardrail/
# 系统正在关闭) 与 provider_manager 的 provider 失败面 (410/429/all providers
# failed/circuit open)。**不含** 业务/配置类词汇 (risk veto/置信度不足/…)。
LLM_DEGRADED_SIGNATURES = (
    "410",             # provider 端点失效 (F-21 现场实证)
    "429",             # 限流
    "timeout",
    "超时",
    "未调llm",          # fallback 兜底 (含大写 LLM, 匹配前统一小写)
    "all providers failed",
    "circuit open",
)


def _match_llm_signature(reason: str) -> bool:
    """degradation_reason 是否命中 LLM 侧特征词。"""
    low = (reason or "").lower()
    return any(sig in low for sig in LLM_DEGRADED_SIGNATURES)


def llm_degraded_state(obj) -> bool | None:
    """返回「本次是否 LLM 侧降级」: True / False / None(不明)。

    优先级 (统筹裁定 sm-a20c9e1cfa53414d):
        ① 对象带 `degraded_fallback` 属性 -> 直接读它 (F-21 并卡后自动生效)
        ② 带 `quality` 且 tier_used == "degraded":
             - degradation_reason 命中 LLM 侧特征词 -> True
             - degradation_reason 明确指向非 LLM (业务/配置) -> False
             - degradation_reason 为空 -> None (不明, 不可推断)
        ③ 其余情况 -> None (不明)

    参数可以是 AgentResponse (LLM 侧) 或 AgentReviewResult (agent_gate 侧);
    本函数只按「有什么字段」判断, 不 import 具体类, 因此对 F-21 并卡前后
    两种对象形态都成立, 也不制造跨层 import 依赖。
    """
    if obj is None:
        return None

    # ① 最权威信号: F-21 的专用标记
    flag = getattr(obj, "degraded_fallback", None)
    if isinstance(flag, bool):
        return flag

    # ② 现有可观测面: quality.tier_used + degradation_reason
    quality = getattr(obj, "quality", None)
    if quality is not None:
        tier = getattr(quality, "tier_used", None)
        if tier == "degraded":
            reason = getattr(quality, "degradation_reason", None)
            if reason is None:
                reason = ""
            reason = str(reason)
            if _match_llm_signature(reason):
                return True
            if reason.strip():
                # 明确有原因但不命中 LLM 特征 -> 业务/配置类降级, 不是 LLM 降级
                return False
            # degraded 但无原因 -> 不可推断, 明说不明
            return None
        # tier 明确不是 degraded -> 确认不是 LLM 侧降级
        if isinstance(tier, str) and tier:
            return False

    # ③ 没有任何可读面 -> 不明
    return None
