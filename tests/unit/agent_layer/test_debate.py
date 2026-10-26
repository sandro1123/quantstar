"""Debate Module 单元测试"""
import pytest
from agent_layer.debate.models import ExpertReport, DebateState, FinalDecision
from agent_layer.debate.experts import SentimentExpert, RiskExpert, TechExpert
from agent_layer.debate.bull_bear import BullAgent, BearAgent
from agent_layer.debate.fulcrum import Fulcrum
from agent_layer.debate.decision_hub import DecisionHub


def test_sentiment_expert_bullish():
    e = SentimentExpert()
    r = e.analyze({"turnover_ratio": 10})
    assert r.score > 0


def test_sentiment_expert_bearish():
    e = SentimentExpert()
    r = e.analyze({"turnover_ratio": 0.5})
    assert r.score < 0


def test_risk_expert():
    e = RiskExpert()
    r = e.analyze({"pe": 150})
    assert r.score < 0


def test_bull_agent():
    reports = [ExpertReport("sentiment", 0.5, ["高换手"]), ExpertReport("tech", 0.3, ["动量正"])]
    agent = BullAgent()
    args = agent.generate(reports)
    assert len(args) > 0


def test_bear_agent():
    reports = [ExpertReport("risk", -0.5, ["高PE"]), ExpertReport("tech", -0.3, ["动量负"])]
    agent = BearAgent()
    args = agent.generate(reports)
    assert len(args) > 0


def test_fulcrum():
    fulcrum = Fulcrum()
    from agent_layer.debate.models import ExpertReport
    reports = [ExpertReport(expert_name="test", score=0.5, key_points=["好"], confidence=0.6)]
    result = fulcrum.analyze(reports, ["看多论据1"], ["看空论据1"])
    assert result is not None
    assert len(result) > 0


def test_decision_hub():
    hub = DecisionHub()
    decision = hub.decide("推荐", {"momentum": 0.5, "value": 0.3})
    assert decision.verdict in ("推荐", "不推荐", "观望")
    assert 0 <= decision.position_pct <= 100


def test_decision_hub_high_position_requires_human():
    hub = DecisionHub()
    decision = hub.decide("偏多", {"momentum": 2.0, "value": 1.5})
    if decision.position_pct > 50:
        assert decision.requires_human is True


def test_decision_hub_low_position_no_human():
    hub = DecisionHub()
    decision = hub.decide("中性", {"momentum": 0.1, "value": 0.0})
    if decision.position_pct <= 50:
        assert decision.requires_human is False


def test_decision_hub_negative():
    hub = DecisionHub()
    decision = hub.decide("偏空", {"momentum": -0.8, "value": -0.5})
    assert decision.verdict == "不推荐"
    assert decision.position_pct == 0
