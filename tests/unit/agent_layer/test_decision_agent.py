"""DecisionAgent 单元测试"""
import pytest
from agent_layer.agents.decision_agent import DecisionAgent


def test_decision_agent_rule_based():
    agent = DecisionAgent(provider_manager=None)
    result = agent.analyze({
        "code": "000001",
        "date": "2026-07-17",
        "factor_scores": {"momentum": 0.8, "value": 0.6},
    })
    assert result.verdict == "推荐"
    assert result.confidence > 0


def test_decision_agent_sell_signal():
    agent = DecisionAgent(provider_manager=None)
    result = agent.analyze({
        "code": "000002",
        "date": "2026-07-17",
        "factor_scores": {"momentum": -0.8, "value": -0.6},
    })
    assert result.verdict == "不推荐"


def test_decision_agent_hold():
    agent = DecisionAgent(provider_manager=None)
    result = agent.analyze({
        "code": "000003",
        "date": "2026-07-17",
        "factor_scores": {"momentum": 0.1, "value": -0.1},
    })
    assert result.verdict == "观望"
