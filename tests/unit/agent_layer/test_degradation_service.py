"""DegradationManager + LLMService 异常路径 单元测试"""
import pytest
from unittest.mock import MagicMock
from agent_layer.llm.degradation import DegradationManager, DegradationTier
from agent_layer.llm.service import LLMService


class TestDegradationManager:
    def test_full_llm(self):
        dm = DegradationManager()
        tier = dm.evaluate(llm_available=True, quota_remaining=200)
        assert tier == DegradationTier.FULL_LLM

    def test_cheap_llm(self):
        dm = DegradationManager()
        tier = dm.evaluate(llm_available=True, quota_remaining=50)
        assert tier == DegradationTier.CHEAP_LLM

    def test_stale_cache_from_exhaustion(self):
        dm = DegradationManager()
        tier = dm.evaluate(llm_available=True, quota_remaining=0, has_stale_cache=True)
        assert tier == DegradationTier.STALE_CACHE

    def test_rule_only_from_exhaustion(self):
        dm = DegradationManager()
        tier = dm.evaluate(llm_available=True, quota_remaining=0, has_stale_cache=False)
        assert tier == DegradationTier.RULE_ONLY

    def test_stale_cache_from_down(self):
        dm = DegradationManager()
        tier = dm.evaluate(llm_available=False, quota_remaining=0, has_stale_cache=True)
        assert tier == DegradationTier.STALE_CACHE

    def test_rule_only_all_failed(self):
        dm = DegradationManager()
        tier = dm.evaluate(llm_available=False, quota_remaining=0, has_stale_cache=False)
        assert tier == DegradationTier.RULE_ONLY

    def test_force_tier(self):
        dm = DegradationManager()
        dm.force_tier(DegradationTier.CHEAP_LLM, "manual override")
        assert dm.current_tier == DegradationTier.CHEAP_LLM
        assert dm.reason == "manual override"

    def test_reason_property(self):
        dm = DegradationManager()
        dm.evaluate(llm_available=True, quota_remaining=50)
        assert dm.reason == "quota_low"


class TestLLMServiceExceptions:
    def test_call_no_provider(self):
        svc = LLMService(provider_manager=None)
        result = svc.call("test prompt")
        assert result is None

    def test_call_no_available_llm(self):
        pm = MagicMock()
        pm.has_available_llm.return_value = False
        svc = LLMService(provider_manager=pm)
        result = svc.call("test")
        assert result is None

    def test_call_success(self):
        pm = MagicMock()
        pm.has_available_llm.return_value = True
        pm.call.return_value = "hello"
        svc = LLMService(provider_manager=pm)
        result = svc.call("test")
        assert result == "hello"

    def test_call_exception(self):
        pm = MagicMock()
        pm.has_available_llm.return_value = True
        pm.call.side_effect = RuntimeError("fail")
        svc = LLMService(provider_manager=pm)
        result = svc.call("test")
        assert result is None

    def test_call_json_success(self):
        pm = MagicMock()
        pm.has_available_llm.return_value = True
        pm.call.return_value = '{"key": "value"}'
        svc = LLMService(provider_manager=pm)
        result = svc.call_json("test")
        assert result == {"key": "value"}

    def test_call_json_markdown_block(self):
        pm = MagicMock()
        pm.has_available_llm.return_value = True
        pm.call.return_value = '```json\n{"a": 1}\n```'
        svc = LLMService(provider_manager=pm)
        result = svc.call_json("test")
        assert result == {"a": 1}

    def test_call_json_invalid(self):
        pm = MagicMock()
        pm.has_available_llm.return_value = True
        pm.call.return_value = "not json"
        svc = LLMService(provider_manager=pm)
        result = svc.call_json("test")
        assert result is None

    def test_call_json_none_response(self):
        pm = MagicMock()
        pm.has_available_llm.return_value = True
        pm.call.return_value = None
        svc = LLMService(provider_manager=pm)
        result = svc.call_json("test")
        assert result is None

    def test_call_structured_no_provider(self):
        svc = LLMService(provider_manager=None)
        result = svc.call_structured("test", schema=dict)
        assert result is None

    def test_call_structured_exception(self):
        pm = MagicMock()
        pm.has_available_llm.return_value = True
        pm.call_structured.side_effect = RuntimeError("fail")
        svc = LLMService(provider_manager=pm)
        result = svc.call_structured("test", schema=dict)
        assert result is None

    def test_cache_hit(self):
        pm = MagicMock()
        pm.has_available_llm.return_value = True
        pm.call.return_value = "cached_result"
        svc = LLMService(provider_manager=pm)
        svc.call("prompt1", call_point="test")
        svc.call("prompt1", call_point="test")
        assert pm.call.call_count == 1
