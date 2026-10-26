"""DecisionHub 单元测试 — 含 LLM 路径 + 分歧标记"""
import pytest
from unittest.mock import MagicMock
from agent_layer.debate.decision_hub import DecisionHub
from agent_layer.debate.models import FinalDecision


class TestDecisionHub:
    def test_math_only_recommend(self):
        hub = DecisionHub(provider_manager=None)
        result = hub.decide("推荐", {"factor_a": 0.5, "factor_b": 0.4})
        assert isinstance(result, FinalDecision)
        assert result.verdict == "推荐"
        assert result.position_pct > 0

    def test_math_only_neutral(self):
        hub = DecisionHub(provider_manager=None)
        result = hub.decide("观望", {"factor_a": 0.1, "factor_b": -0.1})
        assert result.verdict == "观望"

    def test_math_only_reject(self):
        hub = DecisionHub(provider_manager=None)
        result = hub.decide("不推荐", {"factor_a": -0.5, "factor_b": -0.4})
        assert result.verdict == "不推荐"
        assert result.position_pct == 0

    def test_llm_divergence(self):
        pm = MagicMock()
        pm.has_available_llm.return_value = True
        pm.call.return_value = "不推荐"
        hub = DecisionHub(provider_manager=pm)
        result = hub.decide("推荐", {"factor_a": 0.5})
        assert "LLM与数学判断分歧" in result.risk_flags

    def test_llm_agreement(self):
        pm = MagicMock()
        pm.has_available_llm.return_value = True
        pm.call.return_value = "推荐"
        hub = DecisionHub(provider_manager=pm)
        result = hub.decide("推荐", {"factor_a": 0.5})
        assert "LLM与数学判断分歧" not in result.risk_flags

    def test_llm_unavailable(self):
        pm = MagicMock()
        pm.has_available_llm.return_value = False
        hub = DecisionHub(provider_manager=pm)
        result = hub.decide("观望", {"factor_a": 0.1})
        assert result.verdict == "观望"

    def test_llm_exception(self):
        pm = MagicMock()
        pm.has_available_llm.return_value = True
        pm.call.side_effect = RuntimeError("LLM down")
        hub = DecisionHub(provider_manager=pm)
        result = hub.decide("观望", {"factor_a": 0.1})
        assert result.verdict == "观望"

    def test_low_factor_triggers_risk_flag(self):
        hub = DecisionHub(provider_manager=None)
        result = hub.decide("观望", {"factor_a": -0.6, "factor_b": -0.5})
        assert "因子评分过低" in result.risk_flags

    def test_high_position_triggers_human(self):
        hub = DecisionHub(provider_manager=None)
        result = hub.decide("推荐", {"factor_a": 0.9, "factor_b": 0.8})
        assert result.requires_human is True
        assert "仓位偏高" in result.risk_flags

    def test_empty_factor_scores(self):
        hub = DecisionHub(provider_manager=None)
        result = hub.decide("观望", {})
        assert result.verdict == "观望"
