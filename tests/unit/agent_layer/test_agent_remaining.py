import pytest
"""Tests for agent layer remaining modules"""
from agent_layer.debate.bull_bear import BullAgent, BearAgent
from agent_layer.debate.experts import SentimentExpert, RiskExpert, ExpertReport
from agent_layer.quality.tracker import QualityTracker
from agent_layer.core.state_machine import StateMachine, AgentCheckpoint, AgentState
from agent_layer.debate.fulcrum import Fulcrum
from common.database import Database
from unittest.mock import MagicMock


class TestStateMachine:
    def test_initial_state(self):
        sm = StateMachine(db=Database(":memory:"))
        assert sm is not None

    def test_checkpoint(self):
        cp = AgentCheckpoint(agent_name="test", task="analysis")
        assert cp.agent_name == "test"
        assert cp.state == AgentState.PENDING


class TestBullAgent:
    def test_generate(self):
        a = BullAgent()
        r = a.generate([])
        assert isinstance(r, list)

class TestBearAgent:
    def test_generate(self):
        a = BearAgent()
        r = a.generate([])
        assert isinstance(r, list)

class TestSentimentExpert:
    def test_analyze(self):
        e = SentimentExpert()
        r = e.analyze({"code": "000001"})
        assert isinstance(r, ExpertReport)

class TestRiskExpert:
    def test_analyze(self):
        e = RiskExpert()
        r = e.analyze({"code": "000001"})
        assert isinstance(r, ExpertReport)

class TestFulcrum:
    def test_analyze(self):
        f = Fulcrum()
        r = f.analyze([], [], [])
        assert isinstance(r, str)

class TestQualityTracker:
    def test_write_prediction(self):
        t = QualityTracker(db=Database(":memory:"))
        t.write_prediction(request_id="r1", agent_name="test", code="000001",
                          date="2026-07-24", verdict="buy", confidence=0.8)
        assert t is not None