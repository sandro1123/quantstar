"""LLM Service — Agent 层门内"叙述柜台" (AG-10)

统一 LLM 门 (AgentLayer.analyze / 本叙述柜台) 内的轻量叙述生成入口, 供报告/ops 等消费方调用。
与决策级 analyze() 的区别: 不写 prediction、不挂状态机、不走辩论; 但仍在门内——
- 过 InputSanitizer (prompt 注入清洗)
- 经 ProviderManager.call (内含 guardrail / 配额 / 熔断 / 降级 / middleware)
- 带缓存 + 计量 (tokens/latency/call_point/consumer_id)

用于: 报告叙述生成、异常诊断、市场环境描述等叙述类任务。
消费方应经 AgentLayer.get_llm_service() 或 bootstrap 注入的共享实例获取, 不得自行实例化 ProviderManager。
"""
import time

from agent_layer.security.sanitizer import InputSanitizer
from common.logging import get_logger

logger = get_logger("agent.llm.service")

class LLMService:
    """Agent 层门内叙述柜台 — 供所有 Layer 使用的统一轻量 LLM 入口"""

    def __init__(self, provider_manager=None):
        self._pm = provider_manager
        self._sanitizer = InputSanitizer()
        self._cache: dict[str, tuple[float, str]] = {}
        self._cache_ttl = 600  # 10 分钟缓存
        # AG-10 计量台账: 叙述调用逐条记录 (tokens/latency/call_point/consumer_id)
        self._metering: list[dict] = []
        self._metering_cap = 2000

    def call(self, prompt: str, system: str = "",
             temperature: float = 0.7, max_tokens: int = 2000,
             call_point: str = "llm_service", consumer_id: str = "") -> str | None:
        """叙述柜台文本调用 — 门内: sanitize + ProviderManager(guardrail/配额/熔断) + 缓存 + 计量"""
        if not self._pm or not self._pm.has_available_llm():
            return None

        # AG-10: 输入清洗 (prompt 注入防御), 与决策级 analyze() 同套 InputSanitizer
        sanitized = self._sanitizer.sanitize({"prompt": prompt, "system": system or ""})
        prompt = sanitized.get("prompt", prompt)
        system = sanitized.get("system", system)

        cache_key = f"{call_point}:{hash(prompt)}"
        cached = self._cache.get(cache_key)
        if cached and time.time() < cached[0]:
            self._record_metering(call_point, consumer_id, 0, prompt, cached[1], cached=True)
            return cached[1]

        start = time.time()
        try:
            result = self._pm.call(
                prompt=prompt,
                system_prompt=system if system else None,
                temperature=temperature,
                max_tokens=max_tokens,
                call_point=call_point,
            )
        except Exception as e:
            logger.warning(f"LLMService.call failed: {e}")
            self._record_metering(call_point, consumer_id, int((time.time() - start) * 1000),
                                  prompt, None, cached=False)
            return None
        latency_ms = int((time.time() - start) * 1000)
        if result:
            self._cache[cache_key] = (time.time() + self._cache_ttl, result)
        self._record_metering(call_point, consumer_id, latency_ms, prompt, result, cached=False)
        return result

    def _record_metering(self, call_point: str, consumer_id: str, latency_ms: int,
                         prompt: str, result: str | None, cached: bool):
        """AG-10 计量: tokens(估)/latency/call_point/consumer_id 逐条入账"""
        record = {
            "ts": time.time(),
            "call_point": call_point,
            "consumer_id": consumer_id or call_point,
            "latency_ms": latency_ms,
            "prompt_tokens_est": max(1, len(prompt) // 3) if prompt else 0,
            "result_tokens_est": max(1, len(result) // 3) if result else 0,
            "cached": cached,
            "ok": result is not None,
        }
        self._metering.append(record)
        if len(self._metering) > self._metering_cap:
            self._metering = self._metering[-self._metering_cap:]

    def get_metering(self, limit: int = 100) -> list[dict]:
        """AG-10: 返回最近 limit 条计量记录"""
        return list(self._metering[-limit:])

    def metering_summary(self) -> dict:
        """AG-10: 计量汇总 (调用数/缓存命中/总延迟/按 consumer 归属)"""
        total = len(self._metering)
        cached_hits = sum(1 for m in self._metering if m["cached"])
        total_latency = sum(m["latency_ms"] for m in self._metering)
        by_consumer: dict[str, int] = {}
        for m in self._metering:
            by_consumer[m["consumer_id"]] = by_consumer.get(m["consumer_id"], 0) + 1
        return {
            "total_calls": total,
            "cache_hits": cached_hits,
            "total_latency_ms": total_latency,
            "by_consumer": by_consumer,
        }

    def call_json(self, prompt: str, system: str = "",
                  call_point: str = "llm_service_json") -> dict | None:
        """带 JSON 解析的调用"""
        text = self.call(prompt, system, call_point=call_point)
        if not text:
            return None
        try:
            import json
            # 尝试从 markdown code block 中提取 JSON
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0]
            elif "```" in text:
                text = text.split("```")[1].split("```")[0]
            return json.loads(text.strip())
        except Exception:
            return None

    def call_structured(self, prompt: str, schema: type, system: str = "",
                        call_point: str = "llm_service_structured") -> "object | None":
        """带 dataclass 输出的结构化调用"""
        if not self._pm:
            return None
        try:
            return self._pm.call_structured(
                prompt=prompt, schema_class=schema,
                system_prompt=system if system else None,
                call_point=call_point,
            )
        except Exception as e:
            logger.warning(f"LLMService.call_structured failed: {e}")
            return None

    def is_available(self) -> bool:
        return self._pm is not None and self._pm.has_available_llm()

    def health(self) -> dict:
        if not self._pm:
            return {"available": False, "reason": "no provider_manager"}
        return self._pm.health()
