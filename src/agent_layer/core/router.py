"""Agent Router — task + depth + LLM 感知路由 + 准确率反馈"""
from agent_layer.core.models import AgentRequest
from agent_layer.core.registry import get_default_registry
from common.logging import get_logger

logger = get_logger("agent.router")


class AgentRouter:
    def __init__(self, provider_manager=None, registry=None, quality_tracker=None):
        self._pm = provider_manager
        self._registry = registry or get_default_registry()
        self._quality_tracker = quality_tracker

    def _get_accuracy_bonus(self, task: str) -> float:
        if not self._quality_tracker:
            return 0
        try:
            acc = self._quality_tracker.get_accuracy(task)
            if getattr(acc, 'total', 0) == 0:
                return 0  # AG-01: 无评估数据 → 中性, 不假惩罚 (原 accuracy=0.0 → -0.1)
            accuracy = getattr(acc, 'accuracy', 0.5)
            return (accuracy - 0.5) * 0.2
        except Exception:
            return 0

    def resolve(self, request: AgentRequest) -> tuple[str, float]:
        depth = request.depth
        task = request.task

        llm_available = True
        if self._pm is not None:
            try:
                llm_available = self._pm.has_available_llm()
            except Exception:
                pass

        pipelines = self._registry.get_pipelines(task)
        candidates = [p for p in pipelines if p.depth_matches(depth)]
        if not candidates:
            candidates = [p for p in pipelines if p.depth == "normal"]

        for p in candidates:
            if p.requires_llm and not llm_available:
                continue
            return p.name, p.timeout

        if candidates:
            return candidates[0].name, candidates[0].timeout
        return "quick_scan", 5.0
