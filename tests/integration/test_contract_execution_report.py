"""Contract tests: ExecutionLayer ↔ ReportLayer interface compatibility."""

from unittest.mock import MagicMock, patch
import pytest

from delivery_layer.message import Message


class TestReportLayerConstructorContract:
    """Verify ReportLayer accepts all layer dependencies."""

    @pytest.fixture
    def all_deps(self):
        return {
            "config": MagicMock(),
            "data_layer": MagicMock(),
            "strategy_layer": MagicMock(),
            "execution_layer": MagicMock(),
            "agent_layer": MagicMock(),
            "backtest_layer": MagicMock(),
        }

    def test_report_layer_accepts_all_deps(self, all_deps):
        from report_layer import ReportLayer

        rl = ReportLayer(**all_deps)
        assert rl.config is all_deps["config"]
        assert rl.data_layer is all_deps["data_layer"]
        assert rl.strategy_layer is all_deps["strategy_layer"]
        assert rl.execution_layer is all_deps["execution_layer"]
        assert rl.agent_layer is all_deps["agent_layer"]
        assert rl.backtest_layer is all_deps["backtest_layer"]

    def test_report_layer_accepts_none_defaults(self):
        from report_layer import ReportLayer

        rl = ReportLayer()
        assert rl.config is None
        assert rl.data_layer is None
        assert rl.strategy_layer is None
        assert rl.execution_layer is None
        assert rl.agent_layer is None
        assert rl.backtest_layer is None


class TestMessageContract:
    """Verify Message dataclass has required fields."""

    @pytest.fixture
    def sample_message(self):
        return Message(
            content={"summary": "test report"},
            content_type="feishu_card",
            title="Evening Report",
        )

    def test_message_required_fields_exist(self, sample_message):
        assert hasattr(sample_message, "content")
        assert hasattr(sample_message, "content_type")
        assert hasattr(sample_message, "title")

    def test_message_content_is_dict(self, sample_message):
        assert isinstance(sample_message.content, dict)

    def test_message_content_type_is_str(self, sample_message):
        assert isinstance(sample_message.content_type, str)

    def test_message_title_is_str(self, sample_message):
        assert isinstance(sample_message.title, str)

    def test_message_optional_fields(self, sample_message):
        assert hasattr(sample_message, "severity")
        assert hasattr(sample_message, "loudness")
        assert hasattr(sample_message, "message_id")
        assert hasattr(sample_message, "created_at")

    def test_message_severity_default(self):
        msg = Message(content={}, content_type="text", title="t")
        assert msg.severity == "info"

    def test_message_loudness_default(self):
        msg = Message(content={}, content_type="text", title="t")
        assert msg.loudness == "on"

    def test_message_id_auto_generated(self):
        msg = Message(content={}, content_type="text", title="t")
        assert isinstance(msg.message_id, str) and len(msg.message_id) > 0

    def test_message_created_at_auto_generated(self):
        msg = Message(content={}, content_type="text", title="t")
        assert isinstance(msg.created_at, str) and len(msg.created_at) > 0


class TestEveningReportContract:
    """Verify ReportLayer.build_evening_report() returns Message or None."""

    @pytest.fixture
    def report_layer(self):
        from report_layer import ReportLayer
        return ReportLayer(
            config=MagicMock(),
            data_layer=MagicMock(),
            strategy_layer=MagicMock(),
            execution_layer=MagicMock(),
            agent_layer=MagicMock(),
            backtest_layer=MagicMock(),
        )

    def test_build_evening_report_accepts_date_string(self, report_layer):
        with patch.object(report_layer, "_build_report", return_value=None) as mock:
            report_layer.build_evening_report("2025-01-15")
            mock.assert_called_once()

    def test_build_evening_report_returns_message_or_none(self, report_layer):
        result = report_layer.build_evening_report("2025-01-15")
        assert result is None or isinstance(result, Message)

    def test_build_evening_report_when_returns_message(self, report_layer):
        msg = Message(content={"data": 1}, content_type="feishu_card", title="Test")
        with patch.object(report_layer, "_build_report", return_value=msg):
            result = report_layer.build_evening_report("2025-01-15")
            assert isinstance(result, Message)
            assert result.title == "Test"


class TestMorningBriefContract:
    """Verify ReportLayer.build_morning_brief() returns Message or None."""

    @pytest.fixture
    def report_layer(self):
        from report_layer import ReportLayer
        return ReportLayer(
            config=MagicMock(),
            data_layer=MagicMock(),
            strategy_layer=MagicMock(),
            execution_layer=MagicMock(),
            agent_layer=MagicMock(),
            backtest_layer=MagicMock(),
        )

    def test_build_morning_brief_accepts_date_string(self, report_layer):
        with patch.object(report_layer, "_build_report", return_value=None) as mock:
            report_layer.build_morning_brief("2025-01-15")
            mock.assert_called_once()

    def test_build_morning_brief_returns_message_or_none(self, report_layer, monkeypatch):
        from report_layer.sections.overnight_market import OvernightMarketSection
        monkeypatch.setattr(OvernightMarketSection, "_fetch_overnight", lambda self: {})
        result = report_layer.build_morning_brief("2025-01-15")
        assert result is None or isinstance(result, Message)


class TestReportLayerShutdownContract:
    """Verify ReportLayer has shutdown method."""

    def test_shutdown_exists_and_callable(self):
        from report_layer import ReportLayer

        rl = ReportLayer()
        assert callable(getattr(rl, "shutdown", None))

    def test_shutdown_accepts_timeout(self):
        from report_layer import ReportLayer

        rl = ReportLayer()
        rl.shutdown(timeout=5.0)
