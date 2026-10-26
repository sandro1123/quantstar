"""LLM Service 测试 — call / call_json / call_structured / 缓存"""
import pytest
from agent_layer.llm.service import LLMService


class MockProviderManager:
    def __init__(self, available=True, result="test response"):
        self._available = available
        self._result = result

    def has_available_llm(self, quality="low"):
        return self._available

    def call(self, prompt, system_prompt=None, temperature=0.7, max_tokens=500, call_point="llm_service"):
        return self._result

    def call_structured(self, prompt, schema_class, system_prompt=None, call_point="llm_service_structured"):
        return schema_class(response="test")

    def health(self):
        return {"providers_online": 1, "degradation_tier": "full_llm"}


def test_llm_service_call():
    svc = LLMService(provider_manager=MockProviderManager())
    result = svc.call("分析股票", "你是一个分析师")
    assert result == "test response"


def test_llm_service_call_no_provider():
    svc = LLMService(provider_manager=MockProviderManager(available=False))
    result = svc.call("分析股票")
    assert result is None


def test_llm_service_call_no_pm():
    svc = LLMService(provider_manager=None)
    result = svc.call("分析股票")
    assert result is None


def test_llm_service_call_json():
    svc = LLMService(provider_manager=MockProviderManager(result='{"score": 0.8}'))
    result = svc.call_json("分析股票")
    assert result == {"score": 0.8}


def test_llm_service_call_json_markdown_block():
    svc = LLMService(provider_manager=MockProviderManager(result='```json\n{"score": 0.8}\n```'))
    result = svc.call_json("分析股票")
    assert result == {"score": 0.8}


def test_llm_service_call_json_invalid():
    svc = LLMService(provider_manager=MockProviderManager(result="not json"))
    result = svc.call_json("分析股票")
    assert result is None


def test_llm_service_call_json_none():
    svc = LLMService(provider_manager=MockProviderManager(result=None))
    result = svc.call_json("分析股票")
    assert result is None


def test_llm_service_call_structured():
    from dataclasses import dataclass
    @dataclass
    class TestSchema:
        response: str

    svc = LLMService(provider_manager=MockProviderManager())
    result = svc.call_structured("分析股票", TestSchema)
    assert result is not None
    assert result.response == "test"


def test_llm_service_call_structured_no_pm():
    from dataclasses import dataclass
    @dataclass
    class TestSchema:
        response: str

    svc = LLMService(provider_manager=None)
    result = svc.call_structured("分析股票", TestSchema)
    assert result is None


def test_llm_service_cache():
    call_count = [0]

    class CountingProvider(MockProviderManager):
        def call(self, *args, **kwargs):
            call_count[0] += 1
            return "cached"

    svc = LLMService(provider_manager=CountingProvider())
    svc.call("test prompt")
    svc.call("test prompt")
    assert call_count[0] == 1


def test_llm_service_is_available():
    svc = LLMService(provider_manager=MockProviderManager(available=True))
    assert svc.is_available()

    svc2 = LLMService(provider_manager=MockProviderManager(available=False))
    assert not svc2.is_available()

    svc3 = LLMService(provider_manager=None)
    assert not svc3.is_available()


def test_llm_service_health():
    svc = LLMService(provider_manager=MockProviderManager())
    h = svc.health()
    assert h["providers_online"] == 1

    svc2 = LLMService(provider_manager=None)
    h2 = svc2.health()
    assert h2["available"] is False