"""LLM Client — 多 provider + 熔断 + 缓存 + 速率限制"""
import hashlib
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from typing import Any

import requests
import yaml

from common.logging import get_logger

logger = get_logger("agent.llm")


@dataclass
class CostRecord:
    provider: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0
    timestamp: str = ""

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class RateLimiter:
    def __init__(self, rate_per_second: float):
        self.min_interval = 1.0 / max(rate_per_second, 0.1)
        self._lock = threading.Lock()
        self._last_call = 0.0

    def wait(self):
        with self._lock:
            elapsed = time.time() - self._last_call
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
            self._last_call = time.time()


class LLMCache:
    def __init__(self, max_size: int = 500, default_ttl: int = 3600):
        self.max_size = max_size
        self.default_ttl = default_ttl
        self._cache: dict = {}
        self._lock = threading.Lock()

    def _key(self, provider: str, model: str, system: str, user: str) -> str:
        raw = f"{provider}||{model}||{system}||{user}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get(self, key: str) -> str | None:
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            ts, val = entry
            if time.time() - ts > self.default_ttl:
                del self._cache[key]
                return None
            return val

    def set(self, key: str, value: str):
        with self._lock:
            if len(self._cache) >= self.max_size:
                oldest = min(self._cache.keys(), key=lambda k: self._cache[k][0])
                del self._cache[oldest]
            self._cache[key] = (time.time(), value)

    def clear(self):
        with self._lock:
            self._cache.clear()


def parse_json(text: str) -> dict | None:
    text = text.strip()
    text = re.sub(r'^```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```$', '', text).strip()
    start = text.find('{')
    end = text.rfind('}')
    if start == -1 or end == -1 or end <= start:
        return None
    candidate = text[start:end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    try:
        return json.loads(candidate.replace("'", '"'))
    except json.JSONDecodeError:
        return None


def dataclass_schema(cls) -> str:
    if not is_dataclass(cls):
        return "output JSON object."
    parts = [f'    "{f.name}": {str(f.type) if hasattr(f.type, "__name__") else str(f.type)}'
             for f in fields(cls)]
    return "output strict JSON only:\n{\n" + ",\n".join(parts) + "\n}"


def extract_usage_tokens(data) -> int | None:
    """从 OpenAI 兼容响应里取真实 token 数；缺失/畸形返回 None（调用方回落估算）。

    2026-09-24 辩论腿止血配套：此前 provider_manager 记 len(str(result))//3
    把字符数当 token，与真实消耗不同源。改用真实 usage 后额度语义才准。
    """
    if not isinstance(data, dict):
        return None
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None
    total = usage.get("total_tokens")
    if isinstance(total, (int, float)):
        return int(total)
    pt, ct = usage.get("prompt_tokens"), usage.get("completion_tokens")
    if isinstance(pt, (int, float)) and isinstance(ct, (int, float)):
        return int(pt) + int(ct)
    return None


def _effective_max_tokens(prov: dict, requested: int) -> int:
    """G9: 按 provider 的 reasoning_floor 抬升 max_tokens。

    推理模型（如 hy3 系列）先输出 reasoning_content 再输出 content，
    同一个 max_tokens 对它是"思考+回答"的总预算。调用方按普通模型给的
    2000 常常只够思考 —— 实测 cbai/hy3 曾吐 2857 字思考、content 0 字。

    非推理模型不配 reasoning_floor，本函数原样返回 requested（行为不变）。
    """
    try:
        floor = int(prov.get("reasoning_floor") or 0)
    except (TypeError, ValueError):
        floor = 0
    return max(int(requested), floor) if floor > 0 else requested


class LLMClient:
    """LLM 客户端 — 多 provider + 熔断 + 缓存 + 成本追踪"""

    def __init__(self, providers_path: str = None):
        if providers_path is None:
            providers_path = str(
                Path(__file__).parent.parent.parent.parent / "config" / "defaults" / "llm.yaml"
            )
        self._providers_path = providers_path
        self._providers = []
        self._rate_limiters: dict = {}
        self._cache = LLMCache()
        self._circuit_breakers: dict = {}
        self._cost_records: list[CostRecord] = []
        self._last_discovery = 0.0
        self._enable_discovery = True
        self._rotation_lock = threading.Lock()
        self._rotation_index = 0
        self._strict_priority = False
        self._key_source_cache: dict = {}
        self._last_used_provider: str = ""
        self._load_providers()

    def _load_providers(self):
        # CF-06: prefer config registry (llm.yaml already in defaults/ load chain)
        llm_cfg = self._load_from_config()
        if llm_cfg is None:
            # fallback: direct file read (test / config not loaded)
            llm_cfg = self._load_from_file()
        if llm_cfg is None:
            logger.warning("providers config not found (config registry + file)")
            return
        try:
            providers_cfg = llm_cfg.get("providers", [])
            cache_cfg = llm_cfg.get("cache", {})
            self._enable_discovery = bool(llm_cfg.get("enable_discovery", True))
            # 9router 接入: rotation=strict_priority 时按 priority 严格走链不轮换
            # (免费优先付费保底语义要求确定性链序; 缺省保持原轮换行为向后兼容)
            self._strict_priority = llm_cfg.get("rotation") == "strict_priority"
            self._providers = sorted(providers_cfg, key=lambda p: p.get("priority", 99))
            self._cache = LLMCache(
                max_size=cache_cfg.get("max_size", 500),
                default_ttl=cache_cfg.get("default_ttl", 3600),
            )
            for p in self._providers:
                self._rate_limiters[p["name"]] = RateLimiter(p.get("rate_limit", 5))
            if self._enable_discovery:
                self._discover_models()
            logger.info(f"loaded {len(self._providers)} LLM providers")
        except Exception:
            logger.exception("load providers failed")

    def _load_from_config(self):
        """CF-06: read llm section from config registry (get_section rebuilds nested structure)."""
        try:
            from config_layer import get_config
            config = get_config()
            if config._loaded:
                section = config.get_section("llm")
                if section and "providers" in section:
                    return section
        except Exception:
            pass
        return None

    def _load_from_file(self):
        """Fallback: direct file read of llm.yaml."""
        path = Path(self._providers_path)
        if not path.exists():
            return None
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            return raw.get("llm", {})
        except Exception:
            return None

    def reload(self):
        self._load_providers()

    def _ordered_providers(self) -> list:
        """本次调用的 provider 遍历顺序。

        strict_priority (rotation=strict_priority): 严格按 priority 声明序,
        不推进轮换指针 — 免费优先付费保底链要求确定性链序。
        默认: 保留原轮换行为 (每次调用起点+1), 向后兼容。
        """
        if self._strict_priority:
            return list(self._providers)
        with self._rotation_lock:
            start = self._rotation_index % max(len(self._providers), 1)
            self._rotation_index = start + 1
        return self._providers[start:] + self._providers[:start]

    def _resolve_api_key(self, prov: dict) -> str:
        """解析 provider 的 api key。

        优先级: api_key_env 环境变量 > key_source 引用。
        key_source (9router 接入, 统筹裁定 key 不落 .env):
          {type: sqlite, path: <db>, query: <SELECT>} — 只读查询, 结果缓存。
          llm.yaml 只存引用 (path+query, 非秘密), key 值永不进 git。
        """
        env_key = os.environ.get(prov.get("api_key_env", ""), "")
        if env_key:
            return env_key
        # QS_LLM_OFFLINE: 测试套件离线门 (根 conftest 设置, 仿 QS_TEST_DB 先例)。
        # 阻断 key_source 解析生产 9router db 真 key, 防单测真调 localhost:20128。
        # env 显式 key 不受影响 (测试自己 setenv 属显式 opt-in); 生产从不设此变量。
        if os.environ.get("QS_LLM_OFFLINE"):
            return ""
        ks = prov.get("key_source")
        if not ks or ks.get("type") != "sqlite":
            return ""
        cache_key = f"{ks.get('path')}|{ks.get('query')}"
        if cache_key in self._key_source_cache:
            return self._key_source_cache[cache_key]
        value = ""
        try:
            import sqlite3
            conn = sqlite3.connect(f"file:{ks['path']}?mode=ro", uri=True, timeout=5)
            try:
                row = conn.execute(ks["query"]).fetchone()
                if row and row[0]:
                    value = str(row[0])
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"key_source resolve failed for {prov.get('name')}: {e}")
        self._key_source_cache[cache_key] = value
        return value

    def _discover_models(self):
        """从 OpenRouter 发现免费模型并注入 provider 列表"""
        try:
            from agent_layer.llm.discoverers.openrouter import OpenRouterDiscoverer
            api_key = os.environ.get("OPENROUTER_API_KEY", "")
            discovered = OpenRouterDiscoverer().discover(api_key)
            for m in discovered:
                self._providers.append({
                    "name": m["model_id"], "url": "https://openrouter.ai/api/v1/chat/completions",
                    "api_key_env": "OPENROUTER_API_KEY", "model": m["model_id"],
                    "rate_limit": 5, "timeout": 30, "priority": 10,
                })
                self._rate_limiters[m["model_id"]] = RateLimiter(5)
            if discovered:
                logger.info(f"discovered {len(discovered)} free models from OpenRouter")
        except Exception as e:
            logger.debug(f"OpenRouter discover skipped: {e}")

    def ensure_discovery(self):
        """每小时重新发现一次免费模型"""
        import time
        if time.time() - self._last_discovery > 3600:
            self._last_discovery = time.time()
            if not getattr(self, "_enable_discovery", False):
                return
            try:
                import os

                from agent_layer.llm.discoverers.openrouter import OpenRouterDiscoverer
                api_key = os.environ.get("OPENROUTER_API_KEY", "")
                discovered = OpenRouterDiscoverer().discover(api_key)
                existing = {p.get("name") for p in self._providers}
                for m in discovered:
                    if m["model_id"] not in existing:
                        self._providers.append({
                            "name": m["model_id"], "url": "https://openrouter.ai/api/v1/chat/completions",
                            "api_key_env": "OPENROUTER_API_KEY", "model": m["model_id"],
                            "rate_limit": 5, "timeout": 30, "priority": 10,
                        })
                        self._rate_limiters[m["model_id"]] = RateLimiter(5)
                        existing.add(m["model_id"])
                if discovered:
                    logger.info(f"discovery: {len(discovered)} models, total={len(self._providers)}")
            except Exception as e:
                logger.debug(f"discovery skip: {e}")

    @property
    def last_used_provider(self) -> str:
        """Q2: 最近一次成功调用的 provider 名 (降级留痕用)."""
        return self._last_used_provider

    @property
    def provider_names(self) -> list:
        return [p["name"] for p in self._providers]

    def _circuit_open(self, name: str) -> bool:
        entry = self._circuit_breakers.get(name)
        if entry is None:
            return False
        fails, skip_until = entry
        if time.time() < skip_until:
            return True
        self._circuit_breakers[name] = (0, 0)
        return False

    def _circuit_fail(self, name: str):
        entry = self._circuit_breakers.get(name, (0, 0))
        fails = entry[0] + 1
        backoff = [10, 30, 60, 60, 60, 300][min(fails - 1, 5)]
        self._circuit_breakers[name] = (fails, time.time() + backoff)
        logger.warning(f"circuit open: {name}, backoff={backoff}s (fail #{fails})")

    def _circuit_success(self, name: str):
        self._circuit_breakers[name] = (0, 0)

    def chat(self, prompt: str, system_prompt: str = None,
             temperature: float = 0.2, max_tokens: int = 2000,
             cache_ttl: int = None, call_point: str = "unknown",
             timeout: int = None, usage_sink: dict | None = None) -> str | None:
        self.ensure_discovery()
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(
                self._chat_impl, prompt, system_prompt, temperature,
                max_tokens, cache_ttl, call_point, timeout, usage_sink,
            )
            return future.result(timeout=60)
        except TimeoutError:
            logger.warning(f"[{call_point}] chat global timeout 60s")
            return None
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def _chat_impl(self, prompt, system_prompt, temperature, max_tokens,
                   cache_ttl, call_point, timeout, usage_sink=None):
        system_prompt = system_prompt or "你是一位专业的A股市场分析师。"
        use_cache = cache_ttl != 0

        if use_cache:
            for prov in self._providers:
                cache_key = self._cache._key(
                    prov["name"], prov.get("model", ""), system_prompt, prompt
                )
                cached = self._cache.get(cache_key)
                if cached is not None:
                    return cached

        last_error = None
        rotated = self._ordered_providers()
        for prov in rotated:
            pname = prov["name"]
            if self._circuit_open(pname):
                continue

            api_key = self._resolve_api_key(prov)
            if not api_key:
                continue

            rl = self._rate_limiters.get(pname)
            if rl:
                rl.wait()

            for retry in range(2):
                start = time.time()
                success = False
                error_msg = ""
                response_text = None

                try:
                    resp = requests.post(
                        prov["url"],
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": prov.get("model", "default"),
                            "messages": [
                                {"role": "system", "content": system_prompt},
                                {"role": "user", "content": prompt},
                            ],
                            # G9 (2026-10-23): 推理模型的思考会吃掉大部分 token。
                            # 调用方给的 max_tokens 若低于该 provider 的
                            # reasoning_floor, 自动抬到 floor —— 否则会
                            # finish_reason=length + content 空 ("empty response")。
                            # 非推理模型不设 reasoning_floor, 行为逐字节不变。
                            "max_tokens": _effective_max_tokens(prov, max_tokens),
                            "temperature": temperature,
                            # 9router 默认流式输出(SSE/多JSON体), 本客户端只解析纯 JSON
                            "stream": False,
                        },
                        timeout=timeout or prov.get("timeout", 30),
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        content = (
                            data.get("choices", [{}])[0]
                            .get("message", {})
                            .get("content", "")
                        )
                        if content and content.strip():
                            response_text = content.strip()
                            success = True
                            # 2026-09-24: 回填真实 token（usage_sink 为 None 时零行为变化）
                            if usage_sink is not None:
                                _tok = extract_usage_tokens(data)
                                if _tok is not None:
                                    usage_sink["total_tokens"] = _tok
                            if use_cache:
                                for p2 in self._providers:
                                    ck = self._cache._key(
                                        p2["name"], p2.get("model", ""),
                                        system_prompt, prompt,
                                    )
                                    self._cache.set(ck, response_text)
                        else:
                            error_msg = "empty response"
                    else:
                        error_msg = f"HTTP {resp.status_code}: {resp.text[:200]}"
                except Exception as e:
                    error_msg = str(e)[:200]

                latency = int((time.time() - start) * 1000)
                if success:
                    logger.info(f"[{call_point}] {pname} {latency}ms")
                    self._circuit_success(pname)
                    self._last_used_provider = pname
                    return response_text

                logger.warning(f"[{call_point}] {pname} retry {retry}: {error_msg}")
                if retry == 0:
                    time.sleep(2)

            self._circuit_fail(pname)
            last_error = error_msg
            time.sleep(1)

        logger.warning(f"[{call_point}] all providers failed, last: {last_error}")
        return None

    def chat_structured(self, prompt: str, schema_class,
                        system_prompt: str = None, temperature: float = 0.2,
                        call_point: str = "structured", timeout: int = None) -> Any | None:
        if not is_dataclass(schema_class):
            return None

        schema_instruction = dataclass_schema(schema_class)
        full_prompt = f"{prompt}\n\n{schema_instruction}"
        sys_prompt = system_prompt or "你是一位专业的数据分析师。严格按照要求的 JSON 格式输出。"

        for _attempt in range(2):
            text = self.chat(
                prompt=full_prompt, system_prompt=sys_prompt,
                temperature=temperature, max_tokens=2000, cache_ttl=0,
                call_point=call_point, timeout=timeout,
            )
            if not text:
                return None

            parsed = parse_json(text)
            if parsed is not None:
                instance = self._validate_dataclass(schema_class, parsed)
                if instance is not None:
                    return instance

            full_prompt += "\n\n重要：只输出 JSON，不要任何其他文字。"

        logger.warning(f"[{call_point}] JSON parse failed")
        return None

    @staticmethod
    def _validate_dataclass(cls, data: dict) -> Any | None:
        if not is_dataclass(cls):
            return None
        kwargs = {}
        for f in fields(cls):
            val = data.get(f.name)
            if val is None:
                default_val = str(f.type)
                if "str" in default_val:
                    val = ""
                elif "float" in default_val:
                    val = 0.0
                elif "int" in default_val:
                    val = 0
                elif "bool" in default_val:
                    val = False
                else:
                    val = ""
            expected = str(f.type)
            if "float" in expected and isinstance(val, (int, float)):
                val = float(val)
            elif "int" in expected and isinstance(val, (int, float)):
                val = int(val)
            elif "str" in expected and not isinstance(val, str):
                val = str(val)
            kwargs[f.name] = val
        try:
            return cls(**kwargs)
        except Exception:
            return None


    def record_cost(self, provider: str, model: str, prompt_tokens: int = 0, completion_tokens: int = 0):
        from datetime import datetime
        record = CostRecord(
            provider=provider, model=model,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            cost=prompt_tokens * 0.000001 + completion_tokens * 0.000002,
            timestamp=datetime.now().isoformat(),
        )
        self._cost_records.append(record)
        if len(self._cost_records) > 1000:
            self._cost_records = self._cost_records[-500:]

    def get_cost_summary(self) -> dict:
        total_tokens = sum(r.total_tokens for r in self._cost_records)
        total_cost = sum(r.cost for r in self._cost_records)
        return {
            "total_calls": len(self._cost_records),
            "total_tokens": total_tokens,
            "total_cost": round(total_cost, 6),
            "avg_tokens_per_call": total_tokens // max(len(self._cost_records), 1),
        }

    def cleanup(self):
        self._cost_records.clear()
        self._cache.clear()
        logger.info("LLMClient cleaned up")


_instance = None
_lock = threading.Lock()


def get_llm_client() -> LLMClient:
    global _instance
    if _instance is None:
        with _lock:
            if _instance is None:
                _instance = LLMClient()
    return _instance
