"""F5.3 统一 LLM helper — report sections 调 LLM 的唯一入口

约定: 失败/不可用/超时一律返回 None, 调用方走规则降级 (降级路径必须保留)。
AG-10: LLMService 只从 context.llm_service (bootstrap 注入的门内叙述柜台共享实例) 获取,
不再伸手 agent_layer._pm 兜底, 也不在 section 内现场创建 ProviderManager, 避免双 key 轮转池。
"""
import concurrent.futures
import re

from common.logging import get_logger
from agent_layer import build_request
from report_layer.sections.base import note_degraded

logger = get_logger("report.llm_helper")

# AG-WIRE-3 (统筹 2026-09-08): 轨 B — multi_agent_review 辩论链接线。
# 5 旁路点回归体系的第一轨: 报告节带 task= 标记时, 走 agent 层辩论链
# (六专家+多空+裁决官) 取叙述; 任何失败/超时回落轨 A (llm_call 原轨)。
# 独立超时 45s: 报告总闸 60s, 辩论真调实测 33.7s, 留 15s 回落余量。
_REVIEW_TIMEOUT_S = 45


def _review_via_agent(context, prompt: str, task: str, style: str,
                      max_words: int, focus: list[str]) -> str | None:
    """轨 B: agent 层 multi_agent_review 辩论链。失败/超时返回 None → 回落轨 A。"""
    agent = getattr(context, "agent_layer", None)
    if agent is None:
        return None
    try:
        req = build_request(
            task=task,
            input={"prompt": prompt, "date": getattr(context, "date", "")},
            depth="deep",
            style=style, max_words=max_words, focus=focus,
            consumer="report",
        )
        resp = agent.analyze(req)
        if resp is None:
            return None
        # 辩论链叙述出口 = reasoning (裁决官终审叙述); verdict/confidence 结构面
        # 由 write_prediction 留痕 agent_predictions (AG-WIRE-3 验收键)
        text = getattr(resp, "reasoning", None) or ""
        if text and text.strip():
            return clean_markdown(text)
        return None
    except Exception as e:
        logger.warning(f"multi_agent_review 轨失败回落 llm_call: {e}")
        return None


_DEFAULT_TIMEOUT_S = 20

def get_llm(context):
    """从 context 取门内叙述柜台 (bootstrap 注入); 无则返回 None (不新建、不伸手 _pm)"""
    return getattr(context, "llm_service", None)

def llm_call(context, prompt: str, call_point: str,
             temperature: float = 0.5, max_tokens: int = 2000,
             timeout_s: int = _DEFAULT_TIMEOUT_S,
             task: str = "", style: str = "professional_advisor",
             max_words: int = 200, focus: list[str] | None = None) -> str | None:
    """带超时的 LLM 文本调用; 任何失败返回 None → 调用方规则降级。

    AG-WIRE-3 双轨 (统筹 2026-09-08): task 非空 → 先走 multi_agent_review
    辩论链 (轨 B), 失败/超时/无 agent_layer 回落本轨 (叙述柜台直调)。
    既有调用点不带 task 实参 → 行为零变化 (5 旁路点逐节开闸原则)。
    """
    if task:
        reviewed = _review_via_agent(context, prompt, task, style,
                                     max_words, focus or [])
        if reviewed:
            return reviewed
    llm = get_llm(context)
    if llm is None:
        return None
    try:
        if not llm.is_available():
            return None
    except Exception as _deg_exc:
        note_degraded("_llm_helper", _deg_exc)
        return None
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(lambda: llm.call(prompt, temperature=temperature,
                                             max_tokens=max_tokens, call_point=call_point))
            result = fut.result(timeout=timeout_s)
        if result:
            return clean_markdown(result)
        return None
    except concurrent.futures.TimeoutError:
        logger.warning(f"LLM call timeout: {call_point}")
        return None
    except Exception as e:
        logger.warning(f"LLM call failed: {call_point}: {e}")
        return None

def clean_markdown(text: str) -> str:
    """去掉 markdown 标题/加粗符号 (飞书卡片不渲染 md 标题)"""
    return re.sub(r"#{1,6}\s*\*?", "", text.strip())
