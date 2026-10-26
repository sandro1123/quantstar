"""Provider Manager — 路由 + 熔断 + 降级 + 调用 + PromptAdapter + TokenBudget + Guardrail"""
import time

from agent_layer._models import ProviderHealthReport
from agent_layer.llm.client import LLMClient
from agent_layer.llm.degradation import DegradationManager, DegradationTier
from agent_layer.llm.middleware import LoggingMiddleware, MiddlewareChain
from agent_layer.llm.prompt_adapter import PromptAdapter
from agent_layer.llm.provider_pool import ProviderPool
from agent_layer.llm.quota_manager import QuotaManager
from agent_layer.llm.token_budget import TokenBudget
from agent_layer.security.guardrail import Guardrail
from common.logging import get_logger

logger = get_logger("agent.llm.manager")


def _ledger_write(call_point: str, result, usage_sink: dict) -> None:
    """独立短连接写一行用量流水。**任何失败都吞成 warning，绝不炸主链。**

    独立短连接（统筹④）：不与主链共用长事务，flock 饿死案判例。
    库不存在（测试/裸环境）→ 静默跳过（非故障）。
    """
    try:
        import os
        import sqlite3
        from agent_layer.llm.usage_ledger import (
            record_usage_row, ensure_ledger_table)
        path = os.environ.get("QS_LEDGER_DB", "data/qs.db")
        if not os.path.exists(path):
            return
        conn = sqlite3.connect(path, timeout=2.0)
        try:
            ensure_ledger_table(conn)
            tok = usage_sink.get("total_tokens")
            from common.utils import bj_date
            record_usage_row(
                conn, date=bj_date(), call_point=call_point,
                tokens=tok if tok is not None else max(1, len(str(result)) // 3),
                source="real" if tok is not None else "estimated")
        finally:
            conn.close()
    except Exception as e:              # noqa: BLE001 — 留痕绝不炸主链
        logger.warning(f"usage_ledger 写点失败(不阻塞主链): "
                       f"call_point={call_point}: {e}")


class ProviderManager:
    def __init__(self, client: LLMClient = None, config=None):
        self.client = client or LLMClient()
        self.pool = ProviderPool()
        self.degradation = DegradationManager()
        self.prompt_adapter = PromptAdapter()
        self.middleware = MiddlewareChain()
        self.middleware.add(LoggingMiddleware())
        self.token_budget = TokenBudget()
        self.guardrail = Guardrail()
        self.quota_manager = QuotaManager()
        # 微卡 (09-12 配置普查对拍): ops.agent.quota 五键接线半程收口 —
        # 构造点直灌 configure (D-24 execution.initial_capital 先例同款)。
        # 修复前: configure 生产零调用, 走 _DEFAULT_SOURCE_PATH 同值硬编码
        # 兜底 = 默合假象 (yaml 改值不生效, 键语义漂移活标本)。
        cfg_get = (config.get if hasattr(config, "get") else lambda k, d=None: d) if config else (lambda k, d=None: d)
        self.quota_manager.configure(
            daily_budget_requests=cfg_get("ops.agent.quota.daily_budget_requests"),
            daily_budget_cost=cfg_get("ops.agent.quota.daily_budget_cost"),
            source_path=cfg_get("ops.agent.quota.source_path"),
            api_key_hint=cfg_get("ops.agent.quota.api_key_hint"),
            low_remaining_pct=cfg_get("ops.agent.quota.low_remaining_pct"),
        )
        self._init_pool()

    def _init_pool(self):
        for name in self.client.provider_names:
            self.pool.register(name, quota=1000)
            self.quota_manager.register(name, total=1000)

    def call_via_router(self, prompt: str, call_point: str = "router",
                        system_prompt: str = None, temperature: float = 0.2,
                        max_tokens: int = 2000, timeout: int = None) -> str | None:
        """强制走 AgentLayer 路由的 LLM 调用 — 含 Guardrail + TokenBudget + Middleware + Degradation"""
        return self.call(prompt, system_prompt=system_prompt, temperature=temperature,
                         max_tokens=max_tokens, call_point=call_point, timeout=timeout)

    def call(self, prompt: str, system_prompt: str = None,
             temperature: float = 0.2, max_tokens: int = 2000,
             call_point: str = "manager", timeout: int = None) -> str | None:
        # Guardrail 输入端: 只拦 PII (新闻里的「内幕交易立案」等风险词必须送达模型)
        guard = getattr(self.guardrail, "check_input", self.guardrail.check)(prompt)
        if not guard.passed:
            logger.warning(f"[{call_point}] guardrail blocked input: {guard.flagged}")
            return None

        # TokenBudget 检查
        if self.token_budget.is_exceeded(call_point):
            # Day 3 卡二 (2026-10-04): token 旁观者显形 — warning 非 info,
            # alerts 表 直插 (carrier, 判例 #9) 供 18:00 摸 观众/用户 感知
            # debate 真降级 (非静默假绿)。fail-safe: 落库失败仅 warning, 主链不吞。
            logger.warning(
                f"❌ [{call_point}] token budget exhausted — "
                f"LLM 腿 预算耗尽, debate 降级 [DEGRADED:token_budget]")
            try:
                from datetime import datetime
                conn = self.db.get_connection()
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                conn.execute(
                    "INSERT INTO alerts "
                    "(severity, layer, title, detail, action, status, created_at, last_seen) "
                    "VALUES (?, ?, ?, ?, ?, 'open', ?, ?)",
                    ("critical", "agent",
                     f"token budget exhausted: {call_point}",
                     f"call_point={call_point} LLM 腿预算耗尽, debate 降级; "
                     f"明日 budget daily reset 自然恢复",
                     "06:00 晨报审 / 09:00 复盘/Budget 卡 与 累值案八",
                     now, now))
                conn.commit()
            except Exception as _alert_e:
                logger.warning(f"alerts INSERT failed (fail-safe): {_alert_e}")
            return None

        # PromptAdapter 适配
        model_name = self.client.provider_names[0] if self.client.provider_names else ""
        adapted_prompt, adapted_sys = self.prompt_adapter.adapt(prompt, model_name, system_prompt)

        # 降级评估
        has_cache = hasattr(self.client, 'cache') and bool(self.client.cache._cache) if hasattr(self.client, 'cache') else False
        tier = self.degradation.evaluate(
            llm_available=len(self.pool.get_available()) > 0,
            quota_remaining=self.pool.total_remaining(),
            has_stale_cache=has_cache,
        )
        if tier == DegradationTier.RULE_ONLY:
            logger.info(f"[{call_point}] degraded to rule-only")
            return None

        # QuotaManager 选择 provider
        provider = self.quota_manager.select(call_point)
        if not provider:
            logger.info(f"[{call_point}] all providers exhausted")
            return None

        # Middleware 执行
        # 2026-09-24 辩论腿止血配套: usage_sink 回传真实 token;
        # 拿不到时回落字符估算 (口径标记见 TokenBudget.usage_source)。
        _usage_sink: dict = {}
        def call_llm(p: str) -> str | None:
            result = self.client.chat(
                prompt=p, system_prompt=adapted_sys,
                temperature=temperature, max_tokens=max_tokens,
                call_point=call_point, timeout=timeout,
                usage_sink=_usage_sink,
            )
            if result:
                self.quota_manager.record_usage(provider)
            return result

        start = time.time()
        result = self.middleware.execute(adapted_prompt, call_point, call_llm)
        latency_ms = int((time.time() - start) * 1000)

        if result and isinstance(result, str):
            out_guard = getattr(self.guardrail, "check_output", self.guardrail.check)(result)
            if not out_guard.passed:
                logger.warning(f"[{call_point}] guardrail blocked output: {out_guard.flagged}")
                return None

        if result:
            pname = self.client.provider_names[0] if self.client.provider_names else "unknown"
            self.pool.record_success(pname, latency_ms)
            # 真实 token 优先 (provider 回传 usage); 缺失才用字符估算兜底。
            self.token_budget.record_usage(
                call_point, text=str(result),
                real_tokens=_usage_sink.get("total_tokens"))
            # 留痕腿 (2026-09-25): write-through 落库。TokenBudget 是进程内实例,
            # 而每条 cron pipeline 各起进程 -> 用量随进程消失; 故必须每次记账即落库。
            # 统筹④: 独立短连接 + 失败即弃 (不与主链共用长事务, flock 饿死案判例);
            # 写失败 warning 带上下文, 绝不炸主链。
            _ledger_write(call_point, result, _usage_sink)
        else:
            for name in self.client.provider_names:
                self.pool.record_failure(name)

        return result

    def call_structured(self, prompt: str, schema_class,
                        system_prompt: str = None, call_point: str = "structured"):
        return self.client.chat_structured(
            prompt=prompt, schema_class=schema_class,
            system_prompt=system_prompt, call_point=call_point,
        )

    def has_available_llm(self, quality: str = "low") -> bool:
        min_score = 0.3 if quality == "low" else 0.6
        available = self.pool.get_available(min_score=min_score)
        return len(available) > 0

    def cleanup(self):
        if hasattr(self.client, 'cleanup'):
            self.client.cleanup()
        logger.info("ProviderManager cleaned up")

    def health(self) -> ProviderHealthReport:
        return ProviderHealthReport(
            providers_online=len(self.pool.get_available()),
            providers_total=len(self.client.provider_names),
            degradation_active=self.degradation.current_tier != DegradationTier.FULL_LLM,
            degradation_tier=self.degradation.current_tier.value,
            degradation_reason=self.degradation.reason,
            pool_health=self.pool.health_summary(),
        )
