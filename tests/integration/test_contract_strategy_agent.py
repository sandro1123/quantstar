"""Contract tests: StrategyLayer ↔ AgentLayer interface compatibility."""

from unittest.mock import MagicMock, patch
import pytest

from agent_layer.core.models import AgentRequest, AgentResponse


class TestAgentLayerConstructorContract:
    """Verify AgentLayer accepts database and config."""

    @pytest.fixture
    def mock_database(self):
        return MagicMock()

    @pytest.fixture
    def mock_config(self):
        return MagicMock()

    def test_agent_layer_accepts_database_and_config(self, mock_database, mock_config):
        from agent_layer import AgentLayer

        al = AgentLayer(database=mock_database, config=mock_config)
        assert al._database is mock_database
        assert al._config is mock_config

    def test_agent_layer_accepts_none_defaults(self):
        from agent_layer import AgentLayer

        al = AgentLayer()
        assert al._database is None
        assert al._config is None


class TestAgentRequestContract:
    """Verify AgentRequest dataclass fields."""

    @pytest.mark.parametrize("task", ["fundamental_analysis", "risk_check", "signal_validation"])
    def test_agent_request_required_fields(self, task):
        req = AgentRequest(task=task, input={"code": "600519"})
        assert req.task == task
        assert isinstance(req.input, dict)
        assert req.consumer == "unknown"
        assert isinstance(req.trace_id, str) and len(req.trace_id) > 0

    def test_agent_request_sanitize_input_default(self):
        req = AgentRequest(task="test", input={})
        assert req.sanitize_input is True

    def test_agent_request_depth_default(self):
        req = AgentRequest(task="test", input={})
        assert req.depth == "normal"

    def test_agent_request_format_default(self):
        req = AgentRequest(task="test", input={})
        assert req.format == "structured"


class TestAgentResponseContract:
    """Verify AgentResponse dataclass fields."""

    @pytest.fixture
    def sample_response(self):
        return AgentResponse(
            request_id="trace-001",
            task="fundamental_analysis",
            code="600519",
            output={"summary": "test"},
            verdict="buy",
            confidence=0.85,
            reasoning="Strong fundamentals",
            risk_flags=[],
        )

    def test_agent_response_required_fields(self, sample_response):
        assert sample_response.request_id == "trace-001"
        assert sample_response.task == "fundamental_analysis"
        assert sample_response.code == "600519"

    def test_agent_response_verdict_field(self, sample_response):
        assert isinstance(sample_response.verdict, str)
        assert sample_response.verdict == "buy"

    def test_agent_response_confidence_field(self, sample_response):
        assert isinstance(sample_response.confidence, (int, float))
        assert 0 <= sample_response.confidence <= 1

    def test_agent_response_reasoning_field(self, sample_response):
        assert isinstance(sample_response.reasoning, str)

    def test_agent_response_risk_flags_field(self, sample_response):
        assert isinstance(sample_response.risk_flags, list)

    def test_agent_response_has_quality_metrics(self, sample_response):
        assert hasattr(sample_response, "quality")


class TestAgentLayerAnalyzeContract:
    """Verify AgentLayer.analyze() interface."""

    @pytest.fixture
    def agent_layer(self):
        from agent_layer import AgentLayer
        return AgentLayer(database=MagicMock(), config=MagicMock())

    def test_analyze_returns_agent_response(self, agent_layer):
        with patch.object(agent_layer._router, "resolve", return_value=("normal_analysis", 30)), \
             patch("agent_layer.core.agent_layer.DecisionAgent") as MockAgent:
            mock_result = {
                "verdict": "buy",
                "confidence": 0.8,
                "reasoning": "test",
            }
            MockAgent.return_value.analyze.return_value = mock_result
            req = AgentRequest(task="fundamental_analysis", input={"code": "600519"})
            resp = agent_layer.analyze(req)
            assert isinstance(resp, AgentResponse)

    def test_analyze_response_has_required_fields(self, agent_layer):
        with patch.object(agent_layer._router, "resolve", return_value=("normal_analysis", 30)), \
             patch("agent_layer.core.agent_layer.DecisionAgent") as MockAgent:
            MockAgent.return_value.analyze.return_value = {
                "verdict": "hold",
                "confidence": 0.5,
                "reasoning": "neutral",
            }
            req = AgentRequest(task="test", input={"code": "000001"})
            resp = agent_layer.analyze(req)
            assert hasattr(resp, "verdict")
            assert hasattr(resp, "confidence")
            assert hasattr(resp, "reasoning")
            assert hasattr(resp, "risk_flags")
            assert hasattr(resp, "quality")

    def test_analyze_echoes_request_task(self, agent_layer):
        with patch.object(agent_layer._router, "resolve", return_value=("normal_analysis", 30)), \
             patch("agent_layer.core.agent_layer.DecisionAgent") as MockAgent:
            MockAgent.return_value.analyze.return_value = {
                "verdict": "sell",
                "confidence": 0.3,
                "reasoning": "weak",
            }
            req = AgentRequest(task="risk_check", input={"code": "000002"})
            resp = agent_layer.analyze(req)
            assert resp.task == "risk_check"

    def test_health_returns_dict(self, agent_layer):
        health = agent_layer.health()
        assert health.layer == "agent"
        assert health.status == "green"
