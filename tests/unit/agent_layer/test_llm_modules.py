"""Tests for Agent Layer LLM modules"""
from agent_layer.llm.token_budget import TokenBudget, TASK_BUDGETS
from agent_layer.llm.degradation import DegradationManager, DegradationTier


class TestTokenBudget:
    def test_get_budget_returns_default(self):
        tb = TokenBudget()
        assert tb.get_budget("unknown_task") == TASK_BUDGETS["default"]

    def test_get_budget_for_known_task(self):
        tb = TokenBudget()
        assert tb.get_budget("stock_analysis") == TASK_BUDGETS["stock_analysis"]

    def test_record_usage(self):
        tb = TokenBudget()
        tb.record_usage("stock_analysis", 100)
        assert tb.remaining("stock_analysis") == TASK_BUDGETS["stock_analysis"] - 100

    def test_is_exceeded(self):
        tb = TokenBudget()
        tb.record_usage("quick_check", TASK_BUDGETS["quick_check"])
        assert tb.is_exceeded("quick_check") is True
        assert tb.is_exceeded("stock_analysis") is False

    def test_reset(self):
        tb = TokenBudget()
        tb.record_usage("test", 300)
        tb.reset()
        assert tb.remaining("test") == TASK_BUDGETS["default"]

    def test_reset_task(self):
        tb = TokenBudget()
        tb.record_usage("a", 100)
        tb.record_usage("b", 200)
        tb.reset_task("a")
        assert tb.remaining("a") == TASK_BUDGETS["default"]
        assert tb.remaining("b") == TASK_BUDGETS["default"] - 200

    def test_get_all_usage(self):
        tb = TokenBudget()
        tb.record_usage("test", 100)
        usage = tb.get_all_usage()
        assert "test" in usage
        assert usage["test"]["used"] == 100
        assert usage["test"]["remaining"] == TASK_BUDGETS["default"] - 100


class TestDegradationManager:
    def test_default_tier_is_full(self):
        dm = DegradationManager()
        assert dm.current_tier == DegradationTier.FULL_LLM

    def test_full_llm_when_available(self):
        dm = DegradationManager()
        tier = dm.evaluate(llm_available=True, quota_remaining=500)
        assert tier == DegradationTier.FULL_LLM

    def test_cheap_llm_when_quota_low(self):
        dm = DegradationManager()
        tier = dm.evaluate(llm_available=True, quota_remaining=50)
        assert tier == DegradationTier.CHEAP_LLM

    def test_stale_cache_when_quota_exhausted(self):
        dm = DegradationManager()
        tier = dm.evaluate(llm_available=False, quota_remaining=0, has_stale_cache=True)
        assert tier == DegradationTier.STALE_CACHE

    def test_rule_only_when_quota_exhausted_no_cache(self):
        dm = DegradationManager()
        tier = dm.evaluate(llm_available=False, quota_remaining=0)
        assert tier == DegradationTier.RULE_ONLY

    def test_force_tier(self):
        dm = DegradationManager()
        dm.force_tier(DegradationTier.RULE_ONLY, "manual override")
        assert dm.current_tier == DegradationTier.RULE_ONLY
        assert dm.reason == "manual override"