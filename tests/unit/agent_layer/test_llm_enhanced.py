"""LLM 层增强测试 — PromptAdapter / MiddlewareChain / TokenBudget"""
import pytest
from agent_layer.llm.prompt_adapter import PromptAdapter
from agent_layer.llm.middleware import MiddlewareChain, LoggingMiddleware, CacheMiddleware, StopMiddleware
from agent_layer.llm.token_budget import TokenBudget, TASK_BUDGETS


def test_prompt_adapter_qwen():
    adapter = PromptAdapter()
    prompt, sys = adapter.adapt("分析这只股票", "Qwen2.5-72B")
    assert "请用中文回答" in prompt


def test_prompt_adapter_deepseek():
    adapter = PromptAdapter()
    prompt, sys = adapter.adapt("analyze this stock", "deepseek-chat")
    assert "JSON格式" in prompt


def test_prompt_adapter_llama():
    adapter = PromptAdapter()
    prompt, sys = adapter.adapt("分析这只股票", "llama-3.3-70b")
    assert "Please answer in English" in prompt


def test_prompt_adapter_unknown():
    adapter = PromptAdapter()
    prompt, sys = adapter.adapt("hello", "unknown-model")
    assert prompt == "hello"


def test_middleware_chain():
    chain = MiddlewareChain()
    result = chain.execute("test prompt", "test", lambda p: f"result:{p}")
    assert result == "result:test prompt"


def test_middleware_cache():
    chain = MiddlewareChain()
    result1 = chain.execute("hello", "test", lambda p: "world")
    result2 = chain.execute("hello", "test", lambda p: "world")
    assert result1 == "world"
    assert result2 is not None


def test_token_budget():
    budget = TokenBudget()
    assert budget.get_budget("stock_analysis") == TASK_BUDGETS["stock_analysis"]
    assert budget.get_budget("unknown") == TASK_BUDGETS["default"]
    assert budget.remaining("stock_analysis") == TASK_BUDGETS["stock_analysis"]


def test_token_budget_exceeded():
    budget = TokenBudget()
    budget.record_usage("stock_analysis", TASK_BUDGETS["stock_analysis"] + 100)
    assert budget.is_exceeded("stock_analysis")
    assert budget.remaining("stock_analysis") == 0


def test_token_budget_partial():
    budget = TokenBudget()
    budget.record_usage("stock_analysis", 300)
    assert not budget.is_exceeded("stock_analysis")
    assert budget.remaining("stock_analysis") == TASK_BUDGETS["stock_analysis"] - 300


def test_token_budget_reset():
    budget = TokenBudget()
    budget.record_usage("stock_analysis", TASK_BUDGETS["stock_analysis"] + 100)
    assert budget.is_exceeded("stock_analysis")
    budget.reset()
    assert not budget.is_exceeded("stock_analysis")
    assert budget.remaining("stock_analysis") == TASK_BUDGETS["stock_analysis"]


def test_token_budget_reset_task():
    budget = TokenBudget()
    budget.record_usage("stock_analysis", TASK_BUDGETS["stock_analysis"] + 100)
    budget.record_usage("debate", 300)
    assert budget.is_exceeded("stock_analysis")
    budget.reset_task("stock_analysis")
    assert not budget.is_exceeded("stock_analysis")
    assert budget.remaining("debate") == TASK_BUDGETS["debate"] - 300