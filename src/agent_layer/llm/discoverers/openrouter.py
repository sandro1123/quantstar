"""OpenRouter 自动发现 — 调 API 获取免费模型列表"""
import requests

from common.logging import get_logger

logger = get_logger("agent.llm.discoverer.openrouter")


class OpenRouterDiscoverer:
    def discover(self, api_key: str | None = None) -> list[dict]:
        try:
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            resp = requests.get("https://openrouter.ai/api/v1/models", headers=headers, timeout=15)
            if resp.status_code != 200:
                logger.warning(f"OpenRouter API error: {resp.status_code}")
                return self._fallback()
            models = resp.json()
            free_models = []
            for m in models.get("data", []):
                pricing = m.get("pricing", {})
                prompt_price = float(pricing.get("prompt", 1))
                if prompt_price == 0 or ":free" in m.get("id", ""):
                    free_models.append({
                        "provider": "openrouter",
                        "model_id": m.get("id", ""),
                        "tier": "free",
                        "context_window": m.get("context_length", 4096),
                    })
            logger.info(f"OpenRouter: {len(free_models)} free models found")
            return free_models[:5] if free_models else self._fallback()
        except Exception as e:
            logger.warning(f"OpenRouter discover failed: {e}")
            return self._fallback()

    def _fallback(self) -> list[dict]:
        return [
            {"provider": "openrouter", "model_id": "deepseek/deepseek-chat", "tier": "free", "context_window": 8192},
            {"provider": "openrouter", "model_id": "mistralai/mistral-7b", "tier": "free", "context_window": 8192},
        ]
