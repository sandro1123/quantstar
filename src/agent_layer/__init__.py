"""Agent Layer — 统一分析入口"""
from agent_layer.core.agent_layer import AgentLayer, AgentRequest, AgentResponse, QualityMetrics
from agent_layer.core.registry import AgentRegistry
from agent_layer.core.router import AgentRouter

__all__ = ["AgentLayer", "AgentRegistry", "AgentRequest", "AgentResponse", "AgentRouter", "QualityMetrics"]


# ---- AG-QUOTA-2 件3: build_request 请求规范工厂 (12 消费点收口) ----
# 统一构造: 温度/格式/档位 per consumer-task 默认; AgentRequest 裸构造
# 全库收口进此单一入口 (agent_layer 内部转换 :531 quick_scan 直通不入
# 工厂 — 统筹判读 sm-ba4ff3a3: 入工厂破坏内聚)。

_CONSUMER_DEFAULTS: dict[str, dict] = {
    "execution": {"depth": "quick", "format": "structured"},
    "strategy": {"depth": "deep", "format": "structured"},
    "report": {"depth": "deep", "format": "text"},
    "backtest": {"depth": "normal", "format": "structured"},
    "ops": {"depth": "normal", "format": "text"},
}

_TASK_OVERRIDES: dict[str, dict] = {
    "entry_check": {"depth": "quick"},
    "exit_review": {"depth": "quick"},
    "market_analysis": {"depth": "quick"},
    "quick_scan": {"depth": "quick"},
    "deep_review": {"depth": "deep"},
    "multi_agent_review": {"depth": "deep"},
    "expert_debate": {"depth": "deep"},
}


def build_request(task: str, input: dict | None = None,
                  consumer: str = "unknown", depth: str | None = None,
                  style: str | None = None, max_words: int | None = None,
                  focus: list | None = None, context: dict | None = None):
    """AgentRequest 构造单一入口 — 规范温度/格式/档位统一。

    显式实参优先; 缺省按 consumer→task 两级默认。
    input 按 request_profiles.trim_input 裁剪 (token 瘦身, 件2)。
    """
    from agent_layer.core.models import AgentRequest
    from agent_layer.core.request_profiles import trim_input
    defaults = {**_CONSUMER_DEFAULTS.get(consumer, {}),
                **_TASK_OVERRIDES.get(task, {})}
    kwargs = {"format": defaults.get("format", "structured"),
              "depth": depth or defaults.get("depth", "normal"),
              "consumer": consumer}
    if style is not None:
        kwargs["style"] = style
    if max_words is not None:
        kwargs["max_words"] = max_words
    if focus is not None:
        kwargs["focus"] = focus
    if context is not None:
        kwargs["context"] = context
    trimmed = trim_input(task, input or {})
    return AgentRequest(task=task, input=trimmed, **kwargs)
