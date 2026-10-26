"""LLM Client 单元测试"""
import pytest
import time
from agent_layer.llm.client import LLMClient, LLMCache, parse_json, RateLimiter


def test_cache_hit():
    cache = LLMCache(max_size=10, default_ttl=3600)
    key = cache._key("prov", "model", "sys", "prompt")
    cache.set(key, "cached_value")
    assert cache.get(key) == "cached_value"


def test_cache_expiry():
    cache = LLMCache(max_size=10, default_ttl=0)
    key = cache._key("prov", "model", "sys", "prompt")
    cache.set(key, "cached_value")
    time.sleep(0.01)
    assert cache.get(key) is None


def test_cache_lru_eviction():
    cache = LLMCache(max_size=2, default_ttl=3600)
    cache.set("k1", "v1")
    cache.set("k2", "v2")
    cache.set("k3", "v3")
    assert len(cache._cache) == 2
    assert cache.get("k1") is None


def test_parse_json_clean():
    assert parse_json('{"a": 1}') == {"a": 1}
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json("no json here") is None


def test_parse_json_single_quotes():
    result = parse_json("{'key': 'value'}")
    assert result == {"key": "value"}


def test_rate_limiter():
    rl = RateLimiter(rate_per_second=100)
    start = time.time()
    rl.wait()
    rl.wait()
    elapsed = time.time() - start
    assert elapsed >= 0.005


def test_client_init_no_config():
    from config_layer import reset_config
    reset_config()
    client = LLMClient(providers_path="/nonexistent/path.yaml")
    assert client._providers == []
    assert client.provider_names == []


def test_client_circuit_breaker():
    client = LLMClient(providers_path="/nonexistent/path.yaml")
    client._circuit_breakers["test"] = (3, 9999999999999)
    assert client._circuit_open("test") is True
    client._circuit_breakers["test"] = (0, 0)
    assert client._circuit_open("test") is False
