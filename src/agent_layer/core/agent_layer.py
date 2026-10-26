"""AgentLayer 入口 — 接收 AgentRequest → AgentResponse"""
import asyncio
import concurrent.futures
import time
import uuid

from agent_layer._models import AccuracyReport, PredictionRecord, ProviderHealthReport
from agent_layer.agents.decision_agent import DecisionAgent
from agent_layer.core.hooks import LoggingHook, PipelineHook
from agent_layer.core.models import AgentRequest, AgentResponse, QualityMetrics, RiskFlag
from agent_layer.core.output_spec import normalize_verdict
from agent_layer.core.registry import get_default_registry
from agent_layer.core.router import AgentRouter
from agent_layer.core.state_machine import AgentState, StateMachine
from agent_layer.debate.bull_bear import BearAgent, BullAgent
from agent_layer.debate.decision_hub import DecisionHub
from agent_layer.debate.experts import ALL_EXPERTS
from agent_layer.debate.fulcrum import Fulcrum
from agent_layer.llm.prompt_manager import PromptManager
from agent_layer.llm.provider_manager import ProviderManager
from agent_layer.mcp.bridge import MCPBridge
from agent_layer.security import InputSanitizer, OutputValidator
from common.decorators import catch_quantstar_error, with_timeout
from common.logging import get_logger

logger = get_logger("agent")


class AgentLayer:
    def __init__(self, database=None, config=None, data_layer=None, strategy_layer=None):
        self._database = database
        self._config = config
        self._data_layer = data_layer
        self._strategy_layer = strategy_layer
        self._pm = ProviderManager()
        self._llm_service = None  # AG-10: 叙述柜台惰性单例, 经 get_llm_service() 暴露
        self._registry = get_default_registry()
        self._router = AgentRouter(provider_manager=self._pm, registry=self._registry)
        self._sanitizer = InputSanitizer()
        self._validator = OutputValidator()
        self._prompt_manager = PromptManager()
        self._cache: dict[str, tuple[float, AgentResponse]] = {}
        cfg_get = (config.get if hasattr(config, 'get') else lambda k, d=None: d)
        self._cache_ttl = int(cfg_get("agent.cache_ttl_seconds", 300))
        self._max_workers_config = int(cfg_get("agent.max_workers", 8))
        self._analyze_timeout_s = int(cfg_get("agent.analyze_timeout_s", 120))
        self._batch_timeout_s = int(cfg_get("agent.batch_timeout_s", 300))
        from agent_layer.quality.calibrator import Calibrator
        from agent_layer.quality.evaluator import Evaluator
        from agent_layer.quality.memory import AgentMemory
        from agent_layer.quality.tracker import QualityTracker
        self._quality_tracker = QualityTracker(database)
        # AG-01: Router 读侧接质量反馈 — 准确率 bonus 消费评估回填的 hit
        self._router._quality_tracker = self._quality_tracker
        self._memory = AgentMemory(database)
        self._evaluator = Evaluator(database)
        self._calibrator = Calibrator(database)
        quota = self._pm.pool.total_remaining()
        self._max_workers = min(self._max_workers_config, max(1, quota // 200))
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=self._max_workers)
        self._is_shutting_down = False
        self._hooks: list[PipelineHook] = [LoggingHook()]
        self._mcp = self._setup_mcp()
        self._skill_loader = self._setup_skills()
        self._state_machine = StateMachine(database)

    def get_llm_service(self):
        """AG-10 统一入口: 门内叙述柜台。消费方经此取 LLMService, 不伸手 _pm,
        与决策级 analyze() 共享同一 ProviderManager 单池。"""
        if self._llm_service is None:
            from agent_layer.llm.service import LLMService
            self._llm_service = LLMService(provider_manager=self._pm)
        return self._llm_service

    def _setup_mcp(self):
        bridge = MCPBridge()
        try:
            bridge.register_server("china-stock", "stdio", command=["npx", "-y", "@xinkuang/china-stock-mcp"])
            bridge.register_server("a-share", "stdio", command=["npx", "-y", "@kinneyzhang/a-share-mcp"])
            bridge.register_server("tavily", "stdio", command=["npx", "-y", "@tavily/mcp"])
        except Exception as e:
            logger.warning(f"MCP server registration failed (npx/npm not available?): {e}")
        return bridge

    def _setup_skills(self):
        from agent_layer.skills.loader import SkillLoader
        loader = SkillLoader()
        loader.register_skill("stock-analysis", priority=5)
        loader.register_skill("risk-assessment", priority=8)
        return loader

    def _cache_key(self, task: str, code: str, depth: str) -> str:
        return f"{task}|{code}|{depth}"

    def _get_cached(self, task: str, code: str, depth: str) -> AgentResponse | None:
        key = self._cache_key(task, code, depth)
        entry = self._cache.get(key)
        if entry and time.time() < entry[0]:
            return entry[1]
        return None

    def _cache_response(self, task: str, code: str, depth: str, response: AgentResponse):
        key = self._cache_key(task, code, depth)
        self._cache[key] = (time.time() + self._cache_ttl, response)

    @catch_quantstar_error(layer_name="agent")
    def invalidate_cache(self, task: str, code: str | None = None):
        if code:
            keys = [k for k in self._cache if k.startswith(f"{task}|{code}")]
            for k in keys:
                self._cache.pop(k, None)
        else:
            self._cache = {k: v for k, v in self._cache.items() if not k.startswith(f"{task}|")}

    @catch_quantstar_error(layer_name="agent")
    def invalidate_for_news(self, code: str, news_level: str = "重大"):
        """MCP 检测到新闻时失效对应缓存"""
        if news_level in ("重大利好", "重大利空", "紧急"):
            keys = [k for k in self._cache if code in k]
            for k in keys:
                self._cache.pop(k, None)
            logger.info(f"Cache invalidated for {code} due to {news_level} news")

    @catch_quantstar_error(layer_name="agent")
    @with_timeout(timeout_s=120)
    def analyze(self, request: AgentRequest) -> AgentResponse:
        if not request.trace_id:
            request.trace_id = uuid.uuid4().hex[:12]

        code = request.input.get("code", "")
        date = request.input.get("date", "")

        # 从 DataLayer 获取数据（如果可用）
        if self._data_layer and not request.input.get("factor_scores"):
            try:
                snap = self._data_layer.snapshot(date)
                if snap and snap.klines:
                    for k in snap.klines:
                        if k.code == code:
                            request.input["close"] = k.close
                            request.input["volume"] = k.volume
                            request.input["turnover_ratio"] = k.turnover_rate
                            break
            except Exception:
                pass

        # 从 StrategyLayer 获取因子评分（如果可用）
        if self._strategy_layer and not request.input.get("factor_scores"):
            try:
                scores = self._strategy_layer.get_scores(code, date)
                if scores and scores.factors:
                    request.input["factor_scores"] = scores.factors
                    request.input["composite_score"] = scores.composite_score
            except Exception:
                pass

        cached = self._get_cached(request.task, code, request.depth)
        if cached:
            return cached

        code = request.input.get("code", "")
        memory = self._memory.recall(code)
        if memory and request.depth == "quick_scan":
            return AgentResponse(
                request_id=uuid.uuid4().hex[:8], task=request.task, code=code,
                verdict=normalize_verdict(memory["verdict"]), confidence=memory["confidence"],
                reasoning=f"记忆: {memory.get('reasoning', '')}",
                risk_flags=[],
                quality=QualityMetrics(model_used="memory", tier_used="full", latency_ms=0, providers_tried=[]),
            )

        sanitized = self._sanitizer.sanitize(request.input)
        request.input = sanitized

        # Guardrail 检查输入 (只拦 PII; 合规红线作用在模型输出端)
        guardrail = self._pm.guardrail if hasattr(self._pm, 'guardrail') else None
        if guardrail:
            for v in request.input.values():
                if isinstance(v, str):
                    check = getattr(guardrail, "check_input", guardrail.check)
                    gr = check(v)
                    if not gr.passed:
                        logger.warning(f"Guardrail blocked: {gr.flagged}")
                        return self._fallback_response(request, f"Guardrail: {gr.flagged[0][1]}")

        pipeline_name, timeout = self._router.resolve(request)
        logger.info(f"Analyze: task={request.task} depth={request.depth} pipeline={pipeline_name}")

        # 状态机: 创建 Checkpoint → RUNNING
        checkpoint_key = self._state_machine.create(request.task, code, request.trace_id)
        self._state_machine.transition(checkpoint_key, AgentState.RUNNING, f"pipeline={pipeline_name}")

        for hook in self._hooks:
            hook.before_execute(request)

        if self._is_shutting_down:
            logger.warning("AgentLayer analyzing during shutdown, 降级返回")
            return self._fallback_response(request, "系统正在关闭, 已降级")

        future = self._executor.submit(self._execute_pipeline, request, pipeline_name)
        try:
            result = future.result(timeout=timeout)
            self._state_machine.transition(checkpoint_key, AgentState.COMPLETED, "pipeline success")
            self._state_machine.set_result(checkpoint_key, {"verdict": result.verdict, "confidence": result.confidence})
        except concurrent.futures.TimeoutError:
            logger.warning(f"Pipeline {pipeline_name} timeout after {timeout}s")
            result = self._fallback_response(request, f"超时降级 ({pipeline_name} > {timeout}s)")
            self._state_machine.transition(checkpoint_key, AgentState.TIMEOUT, f"timeout after {timeout}s")
            for hook in self._hooks:
                hook.on_error(request, TimeoutError(f"Timeout after {timeout}s"))
        except Exception as e:
            logger.error(f"Pipeline {pipeline_name} failed: {e}")
            result = self._fallback_response(request, f"执行失败: {e}")
            self._state_machine.transition(checkpoint_key, AgentState.FAILED, str(e))
            for hook in self._hooks:
                hook.on_error(request, e)

        for hook in self._hooks:
            hook.after_execute(request, result)

        result = self._validate_output(result)

        # 格式转换
        if request.format == "text":
            result.output = self._format_as_text(result)
        elif request.format == "html":
            result.output = self._format_as_html(result)

        if self._database:
            try:
                result.confidence = self._calibrator.calibrate(pipeline_name, result.confidence)
            except Exception:
                pass
        needs_human = hasattr(result, 'output') and isinstance(result.output, dict) and result.output.get('requires_human')
        if needs_human:
            reasoning = result.reasoning
            from copy import copy
            result = copy(result)
            result.reasoning = reasoning + " [需人工确认]"
        # AG-03: 出口归一化 — 任何管线/缓存/LLM 返回的 verdict 收敛到统一词表
        result.verdict = normalize_verdict(result.verdict)
        self._quality_tracker.write_prediction(
            request_id=result.request_id,
            code=result.code, date=request.input.get("date", ""),
            agent_name=pipeline_name, verdict=result.verdict,
            confidence=result.confidence,
        )
        self._cache_response(request.task, result.code, request.depth, result)
        self._memory.remember(result.code, request.input.get("date", ""), result.verdict, result.confidence, result.reasoning)
        return result


    @property
    def db(self):
        """数据库句柄单一定义点 (2026-09-26 db 幻影修复)。

        根因: __init__ 存 self._database, 而消费方 (persist 落库腿 /
        run_debate 传参) 读 self.db —— 属性从未存在, getattr(self,"db",None)
        静默拿 None: persist 首行静默 return, 死 9 天不抛异常无 warning。
        「防线留了没人接线」第六变体: 接线了, 插头型号不对。
        修复 = property 单一定义点 (优于三处改名 —— 改名后将来还有人
        写 self.db 再踩)。消费方: :583 persist_rationale / :690 run_debate。
        """
        return self._database
    def _execute_pipeline(self, request: AgentRequest, pipeline_name: str) -> AgentResponse:
        if pipeline_name == "deep_debate":
            return self._run_debate(request)
        elif pipeline_name == "quick_scan":
            return self._quick_scan(request)
        elif pipeline_name == "market_analysis":
            return self._market_analysis(request)
        elif pipeline_name == "entry_check":
            return self._entry_check(request)
        elif pipeline_name == "exit_review":
            return self._exit_review(request)
        else:
            return self._normal_analysis(request)

    def _fallback_response(self, request: AgentRequest, reason: str) -> AgentResponse:
        return AgentResponse(
            request_id=uuid.uuid4().hex[:8], task=request.task,
            code=request.input.get("code", ""),
            verdict="观望", confidence=0.3, reasoning=f"{reason}「未调LLM」",
            risk_flags=[], agents_used=["fallback"],
            quality=QualityMetrics(model_used="none", tier_used="degraded", latency_ms=0, providers_tried=[],
                                   degradation_reason=reason),
            degraded_fallback=True,
        )

    def _format_as_text(self, response: AgentResponse) -> str:
        def _rf(r):
            return r.flag if hasattr(r, 'flag') else str(r)
        lines = [
            f"股票: {response.code}",
            f"判断: {response.verdict}",
            f"置信度: {response.confidence:.1%}",
            f"理由: {response.reasoning}",
        ]
        if response.risk_flags:
            lines.append(f"风险: {', '.join(_rf(r) for r in response.risk_flags)}")
        return "\n".join(lines)

    def _format_as_html(self, response: AgentResponse) -> str:
        def _rf(r):
            return r.flag if hasattr(r, 'flag') else str(r)
        risks = "".join(f"<li>{_rf(r)}</li>" for r in response.risk_flags)
        return f"""<div class="agent-response">
<h3>{response.code}: {response.verdict}</h3>
<p>置信度: {response.confidence:.1%}</p>
<p>{response.reasoning}</p>
<ul>{risks}</ul>
</div>"""

    def _run_committee(self, request: AgentRequest) -> AgentResponse:
        code = request.input.get("code", "")
        request.input.get("date", "")
        try:
            from agent_layer.committee.chairman import Chairman
            chairman = Chairman()
            result = chairman.analyze(code, request.input.get("factor_scores", {}))
            return AgentResponse(
                request_id=uuid.uuid4().hex[:8], task=request.task, code=code,
                verdict=result.verdict,
                confidence=result.confidence,
                reasoning=result.reasoning,
                agents_used=["chairman"],
                quality=QualityMetrics(model_used="committee", tier_used="full", latency_ms=0, providers_tried=[]),
            )
        except Exception as e:
            logger.warning(f"Committee failed: {e}")
            return self._fallback_response(request, f"委员会分析失败: {e}")

    @catch_quantstar_error(layer_name="agent")
    @with_timeout(timeout_s=300)
    def analyze_batch(self, requests: list[AgentRequest]) -> list[AgentResponse]:
        if self._is_shutting_down:
            return [self._fallback_response(req, "系统正在关闭, 已降级") for req in requests]
        futures = {self._executor.submit(self.analyze, req): i for i, req in enumerate(requests)}
        results = [None] * len(requests)
        for future in concurrent.futures.as_completed(futures):
            idx = futures[future]
            results[idx] = future.result()
        return results

    @catch_quantstar_error(layer_name="agent")
    async def analyze_async(self, request: AgentRequest) -> AgentResponse:
        if self._is_shutting_down:
            return self._fallback_response(request, "系统正在关闭, 已降级")
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(self._executor, self.analyze, request)

    @catch_quantstar_error(layer_name="agent")
    async def analyze_batch_async(self, requests: list[AgentRequest]) -> list[AgentResponse]:
        tasks = [self.analyze_async(req) for req in requests]
        return await asyncio.gather(*tasks)

    @catch_quantstar_error(layer_name="agent")
    def get_accuracy(self, agent_name: str | None = None, period: str = "monthly") -> AccuracyReport:
        return self._quality_tracker.get_accuracy(agent_name, period)

    @catch_quantstar_error(layer_name="agent")
    def predictions(self, code: str | None = None, date: str | None = None) -> list[PredictionRecord]:
        if not self._database:
            return []
        conn = self._database.get_connection()
        query = "SELECT request_id, agent_name, code, date, verdict, confidence, hit, created_at FROM agent_predictions"
        params = []
        conditions = []
        if code:
            conditions.append("code = ?")
            params.append(code)
        if date:
            conditions.append("date = ?")
            params.append(date)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY created_at DESC"
        try:
            rows = conn.execute(query, params).fetchall()
            return [PredictionRecord(
                request_id=r["request_id"], agent_name=r["agent_name"],
                code=r["code"], date=r["date"], verdict=r["verdict"],
                confidence=r["confidence"], hit=r["hit"], created_at=r["created_at"],
            ) for r in rows]
        except Exception:
            return []

    def health(self):
        from ops_layer.core.models import HealthCheck, LayerHealth
        checks = []

        # 1. LLM provider health
        try:
            provider_health = self.get_provider_health()
            checks.append(HealthCheck(
                name="llm_providers",
                status="pass" if provider_health.providers_online > 0 else "warn",
                detail=f"在线: {provider_health.providers_online}/{provider_health.providers_total}",
                metric=float(provider_health.providers_online),
                threshold=1.0))
            if provider_health.degradation_active:
                checks.append(HealthCheck(
                    name="degradation", status="warn",
                    detail=f"降级中: {provider_health.degradation_tier} ({provider_health.degradation_reason})"))
        except Exception as e:
            checks.append(HealthCheck(name="llm_providers", status="fail", detail=f"Provider检查失败: {e}"))

        # 2. Cache stats
        try:
            cache = getattr(self, "_cache", None)
            if cache is not None and hasattr(cache, "stats"):
                stats = cache.stats()
                hit_rate = stats.get("hit_rate", 0.0)
                cache_status = "pass" if hit_rate > 0.3 else ("warn" if hit_rate > 0 else "fail")
                checks.append(HealthCheck(name="cache", status=cache_status,
                    detail=f"缓存命中率: {hit_rate:.1%}", metric=hit_rate, threshold=0.3))
            else:
                checks.append(HealthCheck(name="cache", status="pass", detail="缓存未启用"))
        except Exception as e:
            checks.append(HealthCheck(name="cache", status="warn", detail=f"缓存检查失败: {e}"))

        # 3. Quality tracker
        try:
            qt = getattr(self, "_quality_tracker", None)
            if qt is not None and hasattr(qt, "accuracy"):
                acc = qt.accuracy()
                qt_status = "pass" if acc >= 0.5 else "warn"
                checks.append(HealthCheck(name="quality", status=qt_status, detail=f"预测准确率: {acc:.1%}"))
        except Exception as e:
            checks.append(HealthCheck(name="quality", status="warn", detail=f"质量检查失败: {e}"))

        # Overall
        status = "green"
        for c in checks:
            if c.status == "fail":
                status = "red"
                break
            elif c.status == "warn":
                status = "yellow"
        return LayerHealth(layer="agent", status=status, checks=checks,
            summary=f"Agent Layer {'OK' if status == 'green' else '需关注'}")

    @catch_quantstar_error(layer_name="agent")
    def get_provider_count(self) -> int:
        return len(self._pm.pool.health_summary()) if hasattr(self._pm, 'pool') else 0

    @catch_quantstar_error(layer_name="agent")
    def get_provider_health(self) -> ProviderHealthReport:
        return self._pm.health() if hasattr(self._pm, 'health') else ProviderHealthReport()

    def start(self):
        logger.info("Agent Layer started")

    def shutdown(self, timeout=10.0):
        self._is_shutting_down = True
        self._executor.shutdown(wait=True, cancel_futures=True)
        self._pm.cleanup() if hasattr(self._pm, 'cleanup') else None
        logger.info("Agent Layer shutdown")

    def _validate_output(self, response: AgentResponse) -> AgentResponse:
        if response.fundamental:
            if response.fundamental.pe and (response.fundamental.pe > 1000 or response.fundamental.pe < 0):
                response.validation_warnings.append(f"PE异常: {response.fundamental.pe}")
        if response.sentiment:
            if response.verdict == "推荐" and response.confidence < 0.3:
                response.validation_warnings.append("低置信度高推荐")
        # AG-03: OutputSpec 契约校验 (告警不阻断)
        from agent_layer.core.output_spec import validate_output_spec
        response.validation_warnings.extend(validate_output_spec(
            response.task,
            {"verdict": response.verdict, "confidence": response.confidence,
             "reasoning": response.reasoning},
        ))
        response.output_validated = True
        return response

    def _normal_analysis(self, request: AgentRequest) -> AgentResponse:
        code = request.input.get("code", "")
        date = request.input.get("date", "")
        factor_scores = request.input.get("factor_scores", {})
        market_regime = request.input.get("market_regime", "neutral")
        analysis_summary = request.input.get("analysis_summary", "")

        # 加载技能
        skill_content = self._skill_loader.select_for_task(request.task)
        if skill_content:
            analysis_summary = (analysis_summary + "\n" + skill_content) if analysis_summary else skill_content

        agent = DecisionAgent(provider_manager=self._pm, prompt_manager=self._prompt_manager)
        result = agent.analyze({
            "code": code, "date": date, "factor_scores": factor_scores,
            "market_regime": market_regime, "analysis_summary": analysis_summary,
        })
        # LLM 返回的 risk_flags 是纯字符串列表, AgentResponse 要求 RiskFlag 对象
        risk_flags = [
            f if isinstance(f, RiskFlag) else RiskFlag(flag=str(f), severity="medium")
            for f in (result.risk_flags or [])
        ]
        return AgentResponse(
            request_id=uuid.uuid4().hex[:8], task=request.task, code=code,
            output=result.output if result.output else {}, verdict=result.verdict,
            confidence=result.confidence, reasoning=result.reasoning,
            risk_flags=risk_flags,
            agents_used=["decision_agent"],
            quality=QualityMetrics(
                model_used=(result.quality or {}).get("model_used", "rule"),
                tier_used=(result.quality or {}).get("tier_used", "full"),
                latency_ms=0, providers_tried=[],
            ),
        )

    def _entry_check(self, request: AgentRequest) -> AgentResponse:
        """AG-03: entry_check 专属管线 — 注册的 EntryCheckAgent 直出 (此前误落 quick_scan)"""
        from agent_layer.agents.entry_check_agent import EntryCheckAgent
        code = request.input.get("code", "")
        agents = self._registry.get_agents("entry_check")
        agent = agents[0] if agents else EntryCheckAgent()
        result = agent.analyze(request.input)
        return AgentResponse(
            request_id=uuid.uuid4().hex[:8], task=request.task, code=code,
            verdict=result.verdict, confidence=result.confidence,
            reasoning=result.reasoning,
            risk_flags=[RiskFlag(flag=f, severity="medium") for f in result.risk_flags],
            agents_used=["entry_check_agent"],
            quality=QualityMetrics(model_used="rule", tier_used="entry_rule", latency_ms=0, providers_tried=[]),
        )

    def _exit_review(self, request: AgentRequest) -> AgentResponse:
        """AG-03: exit_review 专属管线 — 注册的 ExitReviewAgent 直出 (此前误落 quick_scan)"""
        from agent_layer.agents.exit_review_agent import ExitReviewAgent
        code = request.input.get("code", "")
        agents = self._registry.get_agents("exit_review")
        agent = agents[0] if agents else ExitReviewAgent()
        result = agent.analyze(request.input)
        return AgentResponse(
            request_id=uuid.uuid4().hex[:8], task=request.task, code=code,
            verdict=result.verdict, confidence=result.confidence,
            reasoning=result.reasoning,
            risk_flags=[RiskFlag(flag=f, severity="medium") for f in result.risk_flags],
            agents_used=["exit_review_agent"],
            quality=QualityMetrics(model_used="rule", tier_used="exit_rule", latency_ms=0, providers_tried=[]),
        )

    def _quick_scan(self, request: AgentRequest) -> AgentResponse:
        if isinstance(request, str):
            request = AgentRequest(task="quick_scan", input={"date": request})
        code = request.input.get("code", "")
        factor_scores = request.input.get("factor_scores", {})
        composite = sum(factor_scores.values()) / max(len(factor_scores), 1) if factor_scores else 0
        # AG-03: 统一词表 — 原"关注/警惕"归入 推荐/不推荐
        if composite > 0.8:
            verdict, confidence = "推荐", min(0.6 + composite * 0.1, 0.8)
        elif composite < -0.8:
            verdict, confidence = "不推荐", min(0.6 + abs(composite) * 0.1, 0.8)
        else:
            verdict, confidence = "观望", 0.5
        return AgentResponse(
            request_id=uuid.uuid4().hex[:8], task=request.task, code=code,
            verdict=verdict, confidence=confidence,
            reasoning=f"快速扫描: composite={composite:.2f}「未调LLM」",
            risk_flags=[RiskFlag(flag="快速扫描模式，非深度分析", severity="low")],
            agents_used=["quick_scan"],
            quality=QualityMetrics(model_used="rule", tier_used="full", latency_ms=0, providers_tried=[]),
        )

    def _market_analysis(self, request: AgentRequest) -> AgentResponse:
        """AG-11: 市场级分析管线 — MarketAgent 规则核心 + LLM 可选增强"""
        from agent_layer.agents.market_agent import MarketAgent
        code = request.input.get("code", "MARKET")
        date = request.input.get("date", "")
        agent = MarketAgent(data_layer=self._data_layer, provider_manager=self._pm)
        result = agent.analyze({"date": date, "code": code})
        return AgentResponse(
            request_id=uuid.uuid4().hex[:8], task=request.task, code=code,
            output=result.output if result.output else {}, verdict=result.verdict,
            confidence=result.confidence, reasoning=result.reasoning,
            risk_flags=[],
            agents_used=["market_agent"],
            quality=QualityMetrics(
                model_used=result.quality.get("model_used", "rule"),
                tier_used=result.quality.get("tier_used", "market_rule"),
                latency_ms=result.quality.get("latency_ms", 0),
                providers_tried=result.quality.get("providers_tried", []),
            ),
        )

    def _persist_recommendation_rationale(self, final, expert_reports, code: str, date: str) -> None:
        """REC 通电卡: FinalDecision + ExpertReport → recommendation_rationale 落库。

        契约: agent_layer/rationale.py RATIONALE_DDL (1a fe45b0f3 冻结)。
        每条 expert_report 落一行 (UNIQUE(code,rec_date,expert_name));
        claims 从 FinalDecision.reasoning + ExpertReport.key_points 组装
        (每句带 source 锚, 出口硬闸: 可 JOIN 回 trace_id/run_id/request_id)。
        """
        if not date:
            return
        from agent_layer.rationale import record_rationale, RATIONALE_DDL
        db = getattr(self, "db", None)
        if db is None:
            return
        conn = db.get_connection()
        conn.execute(RATIONALE_DDL)
        # claims: 判定理由 + 专家要点, 各句带 source (出口硬闸载体)
        claims = []
        if getattr(final, "reasoning", ""):
            claims.append({"text": final.reasoning,
                           "source": {"kind": "llm", "ref": f"FinalDecision@{code}",
                                      "value": None}})
        for er in (expert_reports or []):
            for kp in (getattr(er, "key_points", None) or []):
                claims.append({"text": kp,
                               "source": {"kind": "expert",
                                          "ref": f"{getattr(er, 'expert_name', '')}@{code}",
                                          "value": None}})
        experts = list(expert_reports or []) or [None]
        for er in experts:
            record_rationale(
                conn, code=code, rec_date=date, decision=final, expert=er,
                claims=claims,
                trace_id=getattr(final, "rationale_id", "") or "",
                run_id="", request_id="")
        conn.commit()

    def _persist_invalidation_warnings(self, expert_reports, code: str,
                                       date: str) -> int:
        """AG-DEBATE-V2 三段: expert 失效条件逐条落库 (每条件一行)。

        消费契约: 次日 ex4 收盘轮按 (code, date) 查表 → 桥版
        advise_with_invalidation 装配警示面。表缺 → OperationalError 由
        调用方 except 捕获降级 (铁律 #12), 主链辩论结果不受影响。
        """
        rows = []
        for r in (expert_reports or []):
            for cond in (getattr(r, "invalidation_conditions", None) or []):
                text = str(cond).strip()[:160]
                if text:
                    rows.append((code, (date or "")[:10],
                                 str(getattr(r, "expert_name", "?")), text))
        if not rows:
            # 空集显式报警 (判例: 空集与零命中必须可区分) ——
            # 原实现静默 return 0, 断线同样不可见。
            logger.warning(
                f"invalidation 空集: expert_reports 无失效条件可落 "
                f"(code={code} date={date}) —— 显式留痕非静默")
            return 0
        conn = self._database.get_connection()
        conn.executemany(
            "INSERT INTO agent_invalidation_warnings "
            "(code, date, expert_name, condition) VALUES (?, ?, ?, ?)", rows)
        conn.commit()
        logger.info(
            f"AG-DEBATE-V2: invalidation 落库 {len(rows)} 行 "
            f"(code={code}, date={(date or '')[:10]})")
        return len(rows)

    def _run_debate(self, request: AgentRequest) -> AgentResponse:
        code = request.input.get("code", "")
        request.input.get("date", "")
        data = request.input.get("data", request.input)
        factor_scores = data.get("factor_scores", {})

        # 委员会模式：直接调 Chairman
        if request.task == "committee":
            return self._run_committee(request)

        # AG-COMPOSE 请求规范三新键 (style/max_words/focus) + 板块/新闻上下文
        # (用户令 2026-09-07: 中短期判断须带市场新闻与板块事件 — 原链 0 处引用)
        req_style = getattr(request, "style", "professional_advisor")
        req_max_words = getattr(request, "max_words", 200)
        req_focus = getattr(request, "focus", []) or []
        style_hint = {
            "professional_advisor": "专业投顾风格, 结论先行, 风险提示收尾。",
            "researcher": "卖方研报风格, 论据分层, 引用数据说话。",
            "risk_officer": "风控官风格, 先讲风险与不利情形, 再谈机会。",
        }.get(req_style, "专业投顾风格。")
        focus_text = "; ".join(str(x) for x in req_focus) if req_focus else "无"

        expert_data = dict(data)
        expert_data["style_hint"] = style_hint
        expert_data["focus"] = focus_text
        expert_data["max_words"] = req_max_words
        expert_data["code"] = code
        expert_data["date"] = data.get("date", "")

        # 板块/新闻上下文注入 (AG-COMPOSE: 原 debate 链 0 处板块引用, 数据面已有
        # sector_fund_flow/stock_news 但从未进专家 prompt — 用户点名中短期重要)
        try:
            ctx_parts = []
            sector_ctx = data.get("sector_context", "")
            if sector_ctx:
                ctx_parts.append(f"[板块动态] {sector_ctx}")
            news_ctx = data.get("news_summary", "")
            if news_ctx:
                ctx_parts.append(f"[近期消息] {news_ctx}")
            if ctx_parts:
                merged = data.get("data_summary", "")
                expert_data["data_summary"] = (merged + "\n" if merged else "") + "\n".join(ctx_parts)
        except Exception:
            pass  # 上下文装配失败不阻断专家腿

        # Layer 1: 6 experts — AG-DEBATE-V2 ①: 1 轮→2 轮制编排核
        # (第 2 轮喂料含第 1 轮全专家摘要 peer_review 互看修订; config
        # agent.debate_rounds 可调, 出厂 2; 裁决面吃 round2 修订后终版)。
        from agent_layer.debate.rounds_orchestrator import rounds_config, run_debate
        _debate_cfg = {"debate_rounds": rounds_config(
            getattr(self, "_debate_rounds_cfg", {}) or {})}
        _orch = run_debate(
            base_data=expert_data, expert_classes=ALL_EXPERTS,
            config=_debate_cfg, provider_manager=self._pm,
            db=getattr(self, "db", None))
        expert_reports = _orch["round2_reports"] or _orch["round1_reports"]
        expert_names = [r.expert_name for r in expert_reports]
        llm_tiers = [getattr(r, "tier_used", "rules") for r in expert_reports]

        # Layer 2: Bull/Bear with adaptive rounds
        bull = BullAgent()
        bear = BearAgent()
        bull_args = bull.generate(expert_reports)
        bear_args = bear.generate(expert_reports)

        debate_history = [{"round": 0, "bull": bull_args, "bear": bear_args}]
        max_rounds = 5
        min_rounds = 2
        for r in range(1, max_rounds + 1):
            bull_rebut = bull.rebut(bear_args, bull_args)
            bear_rebut = bear.rebut(bull_args, bear_args)
            debate_history.append({"round": r, "bull": bull_rebut, "bear": bear_rebut})
            # Early stop: 连续2轮共识不变
            if r >= min_rounds and r >= 2:
                prev = debate_history[-2]
                if bull_rebut == prev.get("bull") and bear_rebut == prev.get("bear"):
                    break

        # Layer 3: Fulcrum + DecisionHub
        fulcrum = Fulcrum()
        fulcrum_verdict = fulcrum.analyze(expert_reports, bull_args, bear_args)

        hub = DecisionHub(provider_manager=self._pm)
        # AG-COMPOSE: 裁决官收全量意见 (六专家+多空+fulcrum) 终审
        final = hub.decide(
            fulcrum_verdict, factor_scores,
            expert_reports=expert_reports,
            bull_args=bull_args, bear_args=bear_args,
        )

        # AG-DEBATE-V2 三段 (2026-09-15): 失效条件出层落库 — 跨腿时序解耦。
        # 二段桥 invalidation_bridge 的 advise_with_invalidation 需要当日
        # expert_reports 对象, 但辩论在 16:00 策略腿 / 收盘轮在 15:00, 当日
        # 对象传不过去 (侦察定谳: 桥生产面 0 调用的根因)。修 = 出口把
        # expert_reports 的 invalidation_conditions 逐条落
        # agent_invalidation_warnings (16:00 辩论落库 → 次日 15:00 收盘轮
        # 按 code+date 查表消费)。表缺/写失败 → warning 不炸主链 (铁律 #12
        # 可观测降级, 辩论结果照常返回)。
        try:
            self._persist_invalidation_warnings(
                expert_reports, code, request.input.get("date", ""))
        except Exception as _inv_e:  # noqa: BLE001 — 落库是附加面, 不炸辩论主链
            logger.warning(
                f"AG-DEBATE-V2: invalidation 落库失败 (不炸主链): {_inv_e}")

        # REC 通电卡 (2026-09-24): LLM 判断产出落库 → recommendation_rationale。
        # 此前 record_rationale 生产零调用 → 表永不建 (懒建病, v10 已入中央清单)。
        # 写法照抄上方 _persist_invalidation_warnings 先例: 落库失败 warning
        # 降级不炸辩论主链 (铁律#12 可观测降级)。
        try:
            self._persist_recommendation_rationale(
                final, expert_reports, code, request.input.get("date", ""))
        except Exception as _rat_e:  # noqa: BLE001 — 落库是附加面, 不炸辩论主链
            logger.warning(
                f"REC: rationale 落库失败 (不炸主链): {_rat_e}")

        # AG-COMPOSE: FinalDecision.risk_flags 是 str 列表, AgentResponse 要
        # RiskFlag 对象 — 照 _run_decision_agent 的先例转换 (修: 首跑全链回执
        # pydantic 校验红 'Input should be a valid ... instance of RiskFlag')。
        final_flags = [
            f if isinstance(f, RiskFlag) else RiskFlag(flag=str(f), severity="medium")
            for f in (final.risk_flags or [])
        ]
        return AgentResponse(
            request_id=uuid.uuid4().hex[:8], task=request.task, code=code,
            verdict=final.verdict, confidence=final.confidence,
            reasoning=final.reasoning, output={"position_pct": final.position_pct},
            risk_flags=final_flags, agents_used=expert_names,
            quality=QualityMetrics(
                model_used="debate",
                tier_used=("llm" if llm_tiers and all(t == "llm" for t in llm_tiers)
                           else ("mixed" if any(t == "llm" for t in llm_tiers) else "rules")),
                latency_ms=0, providers_tried=[],
            ),
        )
