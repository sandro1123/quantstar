"""AgentLayer 集成测试 — data_layer/strategy_layer 接入"""
from agent_layer import AgentLayer, AgentRequest
from unittest.mock import MagicMock


def test_agent_layer_with_data_layer():
    dl = MagicMock()
    sl = MagicMock()
    al = AgentLayer(database=MagicMock(), config=MagicMock(), data_layer=dl, strategy_layer=sl)
    assert al._data_layer is not None
    assert al._strategy_layer is not None


def test_agent_layer_mcp_initialized():
    al = AgentLayer(database=MagicMock(), config=MagicMock())
    health = al._mcp.health()
    assert health["servers_registered"] >= 2
    assert "china-stock" in health["servers"]
    assert "a-share" in health["servers"]


def test_agent_layer_skills_initialized():
    al = AgentLayer(database=MagicMock(), config=MagicMock())
    assert al._skill_loader is not None
    skills = al._skill_loader.list_skills()
    assert isinstance(skills, list)


def test_agent_layer_calibrator_initialized():
    al = AgentLayer(database=MagicMock(), config=MagicMock())
    assert al._calibrator is not None
    assert al._evaluator is not None