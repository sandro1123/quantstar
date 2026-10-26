"""Tests for Agent Layer LLM providers and utilities"""
from agent_layer.llm.quota_manager import QuotaManager
from agent_layer.llm.middleware import MiddlewareChain
from agent_layer.llm.prompt_adapter import PromptAdapter


class TestQuotaManager:
    def test_register_and_remaining(self):
        qm = QuotaManager()
        qm.register("test_provider", total=100)
        assert qm.remaining("test_provider") > 0

    def test_record_usage_reduces_quota(self):
        qm = QuotaManager()
        qm.register("test", total=100)
        before = qm.remaining("test")
        qm.record_usage("test", 10)
        assert qm.remaining("test") < before

    def test_unknown_provider_returns_zero(self):
        qm = QuotaManager()
        assert qm.remaining("nonexistent") == 0


class TestMiddlewareChain:
    def test_chain_execution(self):
        mw = MiddlewareChain()
        result = mw.execute("hello", call_point="test", call_fn=lambda p: "done")
        assert result == "done"


class TestPromptAdapter:
    def test_adapt(self):
        adapter = PromptAdapter()
        prompt, sys_prompt = adapter.adapt("Hello", "qwen")
        assert "中文" in prompt

    def test_adapt_deepseek(self):
        adapter = PromptAdapter()
        prompt, sys_prompt = adapter.adapt("data", "deepseek")
        assert "JSON" in prompt