"""Agent 级 Middleware — LLM 调用前后拦截"""
from typing import Any

from common.logging import get_logger

logger = get_logger("agent.middleware")


class Middleware:
    def before_call(self, prompt: str, call_point: str) -> str:
        return prompt

    def after_call(self, result: Any, call_point: str) -> Any:
        return result


class LoggingMiddleware(Middleware):
    def before_call(self, prompt: str, call_point: str) -> str:
        logger.debug(f"[{call_point}] LLM call, prompt len={len(prompt)}")
        return prompt

    def after_call(self, result: Any, call_point: str) -> Any:
        logger.debug(f"[{call_point}] LLM result len={len(str(result)) if result else 0}")
        return result


class CacheMiddleware(Middleware):
    def __init__(self):
        self._cache: dict[str, str] = {}

    def before_call(self, prompt: str, call_point: str) -> str:
        key = f"{call_point}:{hash(prompt)}"
        cached = self._cache.get(key)
        if cached:
            logger.info(f"[{call_point}] cache hit")
            raise StopMiddleware(cached)
        self._cache_key = key
        return prompt

    def after_call(self, result: Any, call_point: str) -> Any:
        if result and hasattr(self, '_cache_key'):
            self._cache[self._cache_key] = result
        return result


class StopMiddleware(Exception):
    def __init__(self, result: Any):
        self.result = result


class MiddlewareChain:
    def __init__(self):
        self._middlewares: list[Middleware] = [LoggingMiddleware(), CacheMiddleware()]

    def add(self, middleware: Middleware):
        self._middlewares.append(middleware)

    def execute(self, prompt: str, call_point: str, call_fn) -> Any:
        for m in self._middlewares:
            try:
                prompt = m.before_call(prompt, call_point)
            except StopMiddleware as e:
                return e.result

        result = call_fn(prompt)

        for m in reversed(self._middlewares):
            result = m.after_call(result, call_point)
        return result
