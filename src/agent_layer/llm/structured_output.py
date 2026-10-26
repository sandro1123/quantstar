"""P0-6: instructor 结构化输出 — 校验+带错重试层 (传输仍走既有 9router 链)

设计要点 (统筹纪律: LLM 调用走既有 9router 配置不新开通道):
- instructor 的 completion callable 接受任意函数 — 这里传入的适配器把
  ProviderManager.call() 的返回文本包成 ChatCompletion 形对象。
- 因此每次 (重)试都完整走 Guardrail/TokenBudget/PromptAdapter/降级/
  Quota/Middleware/熔断/缓存 全链, instructor 只负责 pydantic 校验与
  把校验错误回灌给模型重试, 不发任何自己的 HTTP。
- 重试时 instructor 把校验错误追加进 user 消息 → prompt 变化 → 缓存键
  不同, 坏响应不会被原样命中。
- 六维验证 (2026-08-27, /tmp/venv-p06): import 13ms/12.4MB; pydantic
  2.13.4 与项目精确一致; litellm 未被拖入; validate+retry 挂自定义
  callable 零真实 HTTP 实证通过。
"""
from types import SimpleNamespace

from pydantic import BaseModel, Field

from common.logging import get_logger

logger = get_logger("agent.llm.structured")


class DecisionVerdict(BaseModel):
    """DecisionAgent 输出契约 — 推荐生成 (全系统最高频 LLM 调用点)"""
    verdict: str = Field(description="推荐 / 不推荐 / 观望")
    confidence: float = Field(ge=0.0, le=1.0, description="置信度 0.0-1.0")
    reasoning: str = Field(default="", description="简短理由")
    risk_flags: list[str] = Field(default_factory=list, description="风险点列表")


def _pm_completion_adapter(pm, *, system_prompt: str, call_point: str,
                           temperature: float, max_tokens: int,
                           timeout: int | None = None):
    """把 ProviderManager.call() 适配成 instructor 的 completion callable。

    返回的 callable 每次被 instructor 调用时都重新走一遍 9router 链;
    instructor 重试追加的校验错误消息会拼进 prompt 让模型自纠。
    """
    def _completion(**kwargs):
        messages = kwargs.get("messages") or []
        # 只保留 user/assistant 内容; system 消息由闭包的 system_prompt
        # 单独传给 pm.call, 此处丢弃避免重复。
        parts = [m.get("content", "") for m in messages
                 if m.get("role") in ("user", "assistant") and m.get("content")]
        text = pm.call(
            prompt="\n\n".join(parts),
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            call_point=call_point,
            timeout=timeout,
        )
        if text is None:
            # 短路: provider 链无响应时不得进 instructor 重试环 — 空内容
            # JSON 校验必败, 会带错重试 max_retries 轮且每轮重走 9router
            # 全链 (build_evening_report 60s 超时元凶)。抛非校验异常,
            # instructor 对此不重试, 一次即冒到 parse 层返 None 交兜底。
            raise RuntimeError(f"[{call_point}] pm.call 返 None, instructor 短路")
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=text or "", role="assistant"),
                finish_reason="stop",
            )],
            usage=SimpleNamespace(prompt_tokens=0, completion_tokens=0, total_tokens=0),
        )
    return _completion


def parse_decision_via_instructor(pm, *, prompt: str, system_prompt: str,
                                  call_point: str, temperature: float = 0.2,
                                  max_tokens: int = 800,
                                  timeout: int | None = None,
                                  max_retries: int = 2) -> DecisionVerdict | None:
    """用 instructor 校验 DecisionAgent 输出; 失败返回 None 交调用方兜底。

    instructor 未安装时返回 None (调用方回退既有 regex/行解析), 不抛异常。
    """
    try:
        import instructor
        from instructor import Mode
    except ImportError:
        logger.debug("instructor 未安装, 回退既有解析路径")
        return None

    client = instructor.from_litellm(
        completion=_pm_completion_adapter(
            pm, system_prompt=system_prompt, call_point=call_point,
            temperature=temperature, max_tokens=max_tokens, timeout=timeout),
        mode=Mode.JSON,
    )
    try:
        return client.chat.completions.create(
            model="qs-9router",  # 仅占位; 真实 model 由 9router 侧决定
            response_model=DecisionVerdict,
            max_retries=max_retries,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
        )
    except Exception as e:
        logger.warning(f"[{call_point}] instructor 结构化解析失败: {e}")
        return None
