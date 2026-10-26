"""降级策略 — Tier 1-4"""
from enum import Enum

from common.logging import get_logger

logger = get_logger("agent.llm.degradation")


class DegradationTier(Enum):
    FULL_LLM = "full"
    CHEAP_LLM = "cheap"
    RULE_ONLY = "rule"
    STALE_CACHE = "stale"


class DegradationManager:
    def __init__(self):
        self._current_tier = DegradationTier.FULL_LLM
        self._reason = None

    @property
    def current_tier(self) -> DegradationTier:
        return self._current_tier

    def evaluate(self, llm_available: bool, quota_remaining: int, has_stale_cache: bool = False) -> DegradationTier:
        if llm_available and quota_remaining > 100:
            self._current_tier = DegradationTier.FULL_LLM
            self._reason = None
        elif llm_available and quota_remaining > 0:
            self._current_tier = DegradationTier.CHEAP_LLM
            self._reason = "quota_low"
        elif quota_remaining <= 0 and has_stale_cache:
            self._current_tier = DegradationTier.STALE_CACHE
            self._reason = "quota_exhausted_using_stale_cache"
        elif quota_remaining <= 0:
            self._current_tier = DegradationTier.RULE_ONLY
            self._reason = "quota_exhausted"
        elif has_stale_cache:
            self._current_tier = DegradationTier.STALE_CACHE
            self._reason = "all_providers_down_using_stale_cache"
        else:
            self._current_tier = DegradationTier.RULE_ONLY
            self._reason = "all_failed"
        return self._current_tier

    def force_tier(self, tier: DegradationTier, reason: str):
        self._current_tier = tier
        self._reason = reason
        logger.warning(f"forced degradation to {tier.value}: {reason}")

    @property
    def reason(self) -> str | None:
        return self._reason
