"""Agent 注册中心 — task → AgentPipeline 映射 + 装饰器 + 缓存"""
import time
from functools import wraps

from agent_layer.agents.base import BaseAgent
from agent_layer.agents.entry_check_agent import EntryCheckAgent
from agent_layer.agents.exit_review_agent import ExitReviewAgent
from agent_layer.agents.intel_agent import IntelAgent
from agent_layer.agents.stock_agent import StockAgent
from agent_layer.core.models import AgentResponse


class AgentPipeline:
    def __init__(self, name: str, task: str, depth: str, requires_llm: bool = False,
                 timeout: float = 30.0, agents: list | None = None):
        self.name = name
        self.task = task
        self.depth = depth
        self.requires_llm = requires_llm
        self.timeout = timeout
        self.agents = agents or []

    def depth_matches(self, depth: str) -> bool:
        return self.depth == depth


class AgentRegistry:
    def __init__(self):
        self._pipelines: dict[str, list[AgentPipeline]] = {}
        self._agents: dict[str, list[BaseAgent]] = {}
        self._cache: dict[str, tuple[float, AgentResponse]] = {}
        self._cache_ttl = 300

    def register(self, task: str, name: str, depth: str = "normal",
                 requires_llm: bool = False, timeout: float = 30.0):
        """装饰器：注册 pipeline"""
        def decorator(fn):
            pipeline = AgentPipeline(name=name, task=task, depth=depth,
                                     requires_llm=requires_llm, timeout=timeout)
            self._pipelines.setdefault(task, []).append(pipeline)
            @wraps(fn)
            def wrapper(*args, **kwargs):
                return fn(*args, **kwargs)
            return wrapper
        return decorator

    def register_pipeline(self, pipeline: AgentPipeline):
        self._pipelines.setdefault(pipeline.task, []).append(pipeline)

    def register_agent(self, task: str, agent: BaseAgent):
        self._agents.setdefault(task, []).append(agent)

    def get_pipelines(self, task: str) -> list[AgentPipeline]:
        return self._pipelines.get(task, [])

    def get_agents(self, task: str) -> list[BaseAgent]:
        return self._agents.get(task, [])

    def _cache_key(self, task: str, code: str, depth: str) -> str:
        return f"{task}|{code}|{depth}"

    def get_cached(self, task: str, code: str, depth: str) -> AgentResponse | None:
        key = self._cache_key(task, code, depth)
        entry = self._cache.get(key)
        if entry and time.time() < entry[0]:
            return entry[1]
        return None

    def cache_response(self, task: str, code: str, depth: str, response: AgentResponse):
        key = self._cache_key(task, code, depth)
        self._cache[key] = (time.time() + self._cache_ttl, response)

    def invalidate_cache(self, task: str, code: str | None = None):
        if code:
            keys = [k for k in self._cache if k.startswith(f"{task}|{code}")]
            for k in keys:
                self._cache.pop(k, None)
        else:
            self._cache = {k: v for k, v in self._cache.items() if not k.startswith(f"{task}|")}


# 注册所有默认 pipeline
_default_registry = AgentRegistry()

def register_pipeline(task: str, name: str, depth: str = "normal",
                      requires_llm: bool = False, timeout: float = 30.0):
    return _default_registry.register(task, name, depth, requires_llm, timeout)

def get_default_registry() -> AgentRegistry:
    pipelines = [
        AgentPipeline("quick_scan", "stock_analysis", "quick", timeout=5.0),
        AgentPipeline("normal_analysis", "stock_analysis", "normal", requires_llm=True, timeout=60.0),
        AgentPipeline("deep_debate", "stock_analysis", "deep", requires_llm=True, timeout=90.0),
        # AG-03: entry_check/exit_review 专属管线 — 规则 agent 直出, 不再借道 quick_scan 个股评分逻辑
        AgentPipeline("entry_check", "entry_check", "quick", requires_llm=False, timeout=5.0),
        AgentPipeline("entry_check", "entry_check", "normal", requires_llm=False, timeout=10.0),
        AgentPipeline("exit_review", "exit_review", "quick", requires_llm=False, timeout=5.0),
        AgentPipeline("exit_review", "exit_review", "normal", requires_llm=False, timeout=10.0),
        AgentPipeline("quick_scan", "quick_check", "quick", timeout=5.0),
        AgentPipeline("deep_debate", "committee", "deep", requires_llm=True, timeout=90.0),
        # AG-11: 市场级分析管线 (规则核心, LLM 可选增强)
        AgentPipeline("market_analysis", "market_analysis", "normal", requires_llm=False, timeout=30.0),
        AgentPipeline("market_analysis", "market_analysis", "quick", requires_llm=False, timeout=10.0),
        # AG-COMPOSE (2026-09-07): 多智能体会诊 — 六专家 LLM 腿 + 多空辩 + 裁决官
        # 走 deep_debate 管线骨架, task 单列便于预算/计量/判读区分。
        AgentPipeline("deep_debate", "multi_agent_review", "deep", requires_llm=True, timeout=120.0),
    ]
    for p in pipelines:
        _default_registry.register_pipeline(p)
    _default_registry.register_agent("entry_check", EntryCheckAgent())
    _default_registry.register_agent("exit_review", ExitReviewAgent())
    _default_registry.register_agent("stock_analysis", StockAgent())
    _default_registry.register_agent("stock_analysis", IntelAgent())
    return _default_registry
