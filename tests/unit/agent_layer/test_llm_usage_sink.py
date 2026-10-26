# -*- coding: utf-8 -*-
"""记账口径接线 TDD — client.chat 回传真实 usage（2026-09-24 辩论腿止血配套）

病案②：provider_manager 记 `max(1, len(str(result)) // 3)` —— 字符数当 token。
修法：client 侧解析响应时 `usage` 字段本就存在，只是被丢弃；加可选 usage_sink
回传，provider_manager 优先用真实值。默认 None → 既有调用行为零变化。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))


class TestUsageSinkContract:
    """usage_sink 是可选的、非侵入的：不传则行为零变化"""

    def test_chat_accepts_usage_sink_kwarg(self):
        import inspect
        from agent_layer.llm.client import LLMClient
        sig = inspect.signature(LLMClient.chat)
        assert "usage_sink" in sig.parameters, "chat 必须接受 usage_sink"
        assert sig.parameters["usage_sink"].default is None, "默认必须为 None（零变化）"

    def test_chat_impl_accepts_usage_sink(self):
        import inspect
        from agent_layer.llm.client import LLMClient
        sig = inspect.signature(LLMClient._chat_impl)
        assert "usage_sink" in sig.parameters

    def test_parses_usage_total_tokens(self):
        """响应含 usage.total_tokens → 回填该值"""
        from agent_layer.llm.client import extract_usage_tokens
        data = {"choices": [{"message": {"content": "x"}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 37,
                          "total_tokens": 137}}
        assert extract_usage_tokens(data) == 137

    def test_usage_absent_returns_none(self):
        """无 usage 字段 → None（调用方回落估算，不静默造数）"""
        from agent_layer.llm.client import extract_usage_tokens
        assert extract_usage_tokens({"choices": [{"message": {"content": "x"}}]}) is None

    def test_usage_malformed_returns_none(self):
        """usage 形状异常（非 dict / 非数值）→ None，不抛"""
        from agent_layer.llm.client import extract_usage_tokens
        assert extract_usage_tokens({"usage": "nope"}) is None
        assert extract_usage_tokens({"usage": {"total_tokens": "abc"}}) is None

    def test_falls_back_to_prompt_plus_completion(self):
        """只有分项没有 total → 相加"""
        from agent_layer.llm.client import extract_usage_tokens
        data = {"usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        assert extract_usage_tokens(data) == 15


class TestProviderManagerUsesRealTokens:
    """provider_manager 记账优先真实值"""

    def test_provider_manager_records_real_tokens_when_available(self):
        """真实 usage 到手 → record_usage 走 real_tokens 口径"""
        from agent_layer.llm.token_budget import TokenBudget, SOURCE_REAL, TASK_BUDGETS
        tb = TokenBudget(today_fn=lambda: "2026-09-24")
        tb.record_usage("expert:tech", real_tokens=137)
        assert tb.usage_source("expert:tech") == SOURCE_REAL
        assert tb.remaining("expert:tech") == TASK_BUDGETS["expert_debate"] - 137

    def test_provider_manager_falls_back_to_estimate(self):
        """无真实值 → 估算口径（不静默：口径标记为 estimated）"""
        from agent_layer.llm.token_budget import TokenBudget, SOURCE_ESTIMATED
        tb = TokenBudget(today_fn=lambda: "2026-09-24")
        tb.record_usage("expert:tech", text="x" * 300)
        assert tb.usage_source("expert:tech") == SOURCE_ESTIMATED
