"""ProviderPool + ProviderManager + Degradation 单元测试"""
import pytest
from agent_layer.llm.provider_pool import ProviderPool, ProviderHealth
from agent_layer.llm.degradation import DegradationManager, DegradationTier
from agent_layer.llm.provider_manager import ProviderManager


def test_provider_health_score():
    h = ProviderHealth(name="test")
    for _ in range(10):
        h.record_success(100)
    assert h.score > 0.8
    assert h.success_rate == 1.0


def test_provider_health_degradation():
    h = ProviderHealth(name="test")
    for _ in range(5):
        h.record_failure()
    assert h.score < 0.5


def test_pool_register_and_available():
    pool = ProviderPool()
    pool.register("prov1", quota=100)
    pool.register("prov2", quota=100)
    available = pool.get_available()
    assert "prov1" in available
    assert "prov2" in available


def test_pool_quota_limit():
    pool = ProviderPool()
    pool.register("prov1", quota=2)
    pool.record_success("prov1", 100)
    pool.record_success("prov1", 100)
    assert "prov1" not in pool.get_available()


def test_degradation_full():
    dm = DegradationManager()
    tier = dm.evaluate(llm_available=True, quota_remaining=500)
    assert tier == DegradationTier.FULL_LLM


def test_degradation_quota_low():
    dm = DegradationManager()
    tier = dm.evaluate(llm_available=True, quota_remaining=50)
    assert tier == DegradationTier.CHEAP_LLM


def test_degradation_no_llm():
    dm = DegradationManager()
    tier = dm.evaluate(llm_available=False, quota_remaining=0)
    assert tier == DegradationTier.RULE_ONLY


def test_provider_manager_has_llm():
    from agent_layer.llm.client import LLMClient
    empty_client = LLMClient.__new__(LLMClient)
    empty_client._providers = []
    empty_client._rate_limiters = {}
    empty_client._cache = None
    empty_client._circuit_breakers = {}
    pm = ProviderManager(client=empty_client)
    assert pm.has_available_llm() is False
