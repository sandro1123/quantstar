"""Debate Module 集成测试 — 完整 _run_debate 流程"""
import pytest
from unittest.mock import MagicMock, patch
from common.database import Database
from agent_layer import AgentLayer, AgentRequest
from agent_layer.core.models import QualityMetrics


@pytest.fixture
def agent_layer():
    db = Database(":memory:")
    al = AgentLayer(database=db, config={})
    yield al
    db.close()


def test_run_debate_full_flow(agent_layer):
    req = AgentRequest(
        task="deep_debate",
        input={
            "code": "000001",
            "date": "2025-06-01",
            "factor_scores": {"momentum": 0.5, "value": 0.3, "volatility": -0.2},
        },
        consumer="test",
        depth="deep",
    )
    resp = agent_layer.analyze(req)
    assert resp.verdict in ("推荐", "不推荐", "观望")
    assert 0 <= resp.confidence <= 1
    assert resp.reasoning is not None
    assert len(resp.reasoning) > 0
    assert resp.agents_used is not None
    assert resp.quality is not None


def test_run_debate_committee(agent_layer):
    req = AgentRequest(
        task="committee",
        input={
            "code": "000001",
            "date": "2025-06-01",
            "factor_scores": {"momentum": 0.5, "value": 0.3},
        },
        consumer="test",
        depth="deep",
    )
    resp = agent_layer.analyze(req)
    assert resp.verdict is not None
    assert resp.confidence >= 0


def test_run_debate_with_scores_high(agent_layer):
    req = AgentRequest(
        task="deep_debate",
        input={
            "code": "600519",
            "date": "2025-06-15",
            "factor_scores": {"momentum": 1.5, "value": 1.2, "quality": 1.0, "growth": 0.8},
        },
        consumer="test",
        depth="deep",
    )
    resp = agent_layer.analyze(req)
    assert resp.verdict is not None
    assert resp.confidence >= 0


def test_run_debate_with_scores_low(agent_layer):
    req = AgentRequest(
        task="deep_debate",
        input={
            "code": "000001",
            "date": "2025-06-01",
            "factor_scores": {"momentum": -1.5, "value": -1.2, "volatility": 1.0},
        },
        consumer="test",
        depth="deep",
    )
    resp = agent_layer.analyze(req)
    assert resp.verdict is not None
    assert resp.confidence >= 0


def test_run_debate_missing_factors(agent_layer):
    req = AgentRequest(
        task="deep_debate",
        input={"code": "000001", "date": "2025-06-01"},
        consumer="test",
        depth="deep",
    )
    resp = agent_layer.analyze(req)
    assert resp.verdict is not None
    assert resp.confidence >= 0


def test_decision_hub_divergence_flag():
    from agent_layer.debate.decision_hub import DecisionHub
    hub = DecisionHub()
    decision = hub.decide("偏多", {"momentum": 1.0, "value": 0.8})
    assert decision.verdict in ("推荐", "不推荐", "观望")
    assert 0 <= decision.position_pct <= 100


def test_fulcrum_analyze():
    from agent_layer.debate.fulcrum import Fulcrum
    from agent_layer.debate.models import ExpertReport
    fulcrum = Fulcrum()
    reports = [
        ExpertReport("sentiment", 0.5, ["高换手率"]),
        ExpertReport("tech", 0.3, ["动量向上"]),
        ExpertReport("risk", -0.2, ["估值偏高"]),
    ]
    result = fulcrum.analyze(reports, ["看多论据"], ["看空论据"])
    assert "偏多" in result or "偏空" in result or "中性" in result