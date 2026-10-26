"""LLM 增强测试 — 回路断指数退避 / 发现器"""
from agent_layer.llm.client import LLMClient
from agent_layer.llm.discoverers.openrouter import OpenRouterDiscoverer


def test_circuit_breaker_initial():
    client = LLMClient()
    name = "test_provider"
    assert name not in client._circuit_breakers


def test_circuit_breaker_backoff():
    client = LLMClient()
    name = "test_provider"
    for i in range(6):
        client._circuit_fail(name)
        fails, until = client._circuit_breakers.get(name, (0, 0))
        assert fails == i + 1
    # 第6次失败后应该是最长退避300s
    _, until = client._circuit_breakers[name]
    import time
    assert until > time.time() + 250


def test_circuit_breaker_success_resets():
    client = LLMClient()
    name = "test_provider"
    client._circuit_fail(name)
    client._circuit_fail(name)
    client._circuit_fail(name)
    client._circuit_success(name)
    fails, until = client._circuit_breakers[name]
    assert fails == 0
    assert until == 0


def test_discoverer_fallback():
    discoverer = OpenRouterDiscoverer()
    models = discoverer._fallback()
    assert len(models) >= 2
    assert models[0]["provider"] == "openrouter"


def test_discoverer_invalid_key():
    discoverer = OpenRouterDiscoverer()
    models = discoverer.discover(api_key="invalid-key-fixture")
    assert len(models) >= 2
    assert models[0]["provider"] == "openrouter"