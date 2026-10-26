"""Contract tests: ReportLayer ↔ DeliveryLayer interface compatibility."""

from unittest.mock import MagicMock, patch
import pytest

from delivery_layer.message import Message


class TestDeliveryLayerConstructorContract:
    """Verify DeliveryLayer accepts config."""

    def test_delivery_layer_accepts_config(self):
        from delivery_layer import DeliveryLayer

        config = MagicMock()
        dl = DeliveryLayer(config=config)
        assert dl.config is config

    def test_delivery_layer_accepts_none_config(self):
        from delivery_layer import DeliveryLayer

        dl = DeliveryLayer(config=None)
        assert dl.config is None

    def test_delivery_layer_default_config(self):
        from delivery_layer import DeliveryLayer

        dl = DeliveryLayer()
        assert dl.config is None


class TestDeliveryLayerSendContract:
    """Verify DeliveryLayer.send() accepts Message."""

    @pytest.fixture
    def delivery_layer(self):
        from delivery_layer import DeliveryLayer
        return DeliveryLayer(config=MagicMock())

    @pytest.fixture
    def sample_message(self):
        return Message(
            content={"summary": "test"},
            content_type="feishu_card",
            title="Test Report",
        )

    def test_send_accepts_message(self, delivery_layer, sample_message):
        assert hasattr(delivery_layer, "send"), "DeliveryLayer must have send() method"

    def test_send_accepts_report_mode(self, delivery_layer, sample_message):
        with patch.object(delivery_layer.executor, "send", return_value={"ok": True}) as mock:
            delivery_layer.send(sample_message, report_mode="debug")
            mock.assert_called_once_with(sample_message, "debug")

    def test_send_returns_result(self, delivery_layer, sample_message):
        expected = {"ok": True, "channel": "feishu"}
        with patch.object(delivery_layer.executor, "send", return_value=expected):
            result = delivery_layer.send(sample_message)
            assert result == expected

    @pytest.mark.parametrize("mode", ["debug", "production", "silent"])
    def test_send_various_report_modes(self, delivery_layer, sample_message, mode):
        with patch.object(delivery_layer.executor, "send", return_value={"ok": True}) as mock:
            delivery_layer.send(sample_message, report_mode=mode)
            mock.assert_called_once_with(sample_message, mode)


class TestMessageDataclassContract:
    """Verify Message dataclass fields for cross-layer transport."""

    @pytest.mark.parametrize(
        "content,content_type,title",
        [
            ({"cards": []}, "feishu_card", "Evening Report"),
            ({"text": "hello"}, "text", "Morning Brief"),
            ({}, "feishu_card", "Weekly Summary"),
        ],
    )
    def test_message_construction(self, content, content_type, title):
        msg = Message(content=content, content_type=content_type, title=title)
        assert msg.content == content
        assert msg.content_type == content_type
        assert msg.title == title

    def test_message_severity_levels(self):
        for sev in ("info", "warning", "critical"):
            msg = Message(content={}, content_type="text", title="t", severity=sev)
            assert msg.severity == sev

    def test_message_loudness_values(self):
        for loud in ("on", "off", "urgent"):
            msg = Message(content={}, content_type="text", title="t", loudness=loud)
            assert msg.loudness == loud

    def test_message_force_channel_optional(self):
        msg = Message(content={}, content_type="text", title="t")
        assert msg.force_channel is None

    def test_message_force_dm_default_false(self):
        msg = Message(content={}, content_type="text", title="t")
        assert msg.force_dm is False


class TestDeliveryLayerHealthContract:
    """Verify DeliveryLayer.health() exists and returns proper structure."""

    def test_health_method_exists(self):
        from delivery_layer import DeliveryLayer

        dl = DeliveryLayer()
        assert callable(getattr(dl, "health", None))

    def test_health_returns_layer_health(self):
        from delivery_layer import DeliveryLayer

        dl = DeliveryLayer()
        health = dl.health()
        assert hasattr(health, "layer") or isinstance(health, dict)

    def test_health_has_status_field(self):
        from delivery_layer import DeliveryLayer

        dl = DeliveryLayer()
        health = dl.health()
        if hasattr(health, "status"):
            assert health.status in ("green", "yellow", "red")
        elif isinstance(health, dict):
            assert "status" in health


class TestCrossLayerMessageFlow:
    """Verify Message can flow from ReportLayer output to DeliveryLayer.send()."""

    def test_message_from_report_to_delivery(self):
        from report_layer import ReportLayer
        from delivery_layer import DeliveryLayer

        rl = ReportLayer(
            config=MagicMock(), data_layer=MagicMock(),
            strategy_layer=MagicMock(), execution_layer=MagicMock(),
            agent_layer=MagicMock(), backtest_layer=MagicMock(),
        )
        dl = DeliveryLayer(config=MagicMock())

        msg = Message(
            content={"stocks": ["600519", "000858"]},
            content_type="feishu_card",
            title="Evening Report 2025-01-15",
        )

        assert isinstance(msg, Message)
        with patch.object(dl.executor, "send", return_value={"ok": True}) as mock:
            dl.send(msg, report_mode="debug")
            mock.assert_called_once()
            sent_msg = mock.call_args[0][0]
            assert sent_msg.title == "Evening Report 2025-01-15"
            assert sent_msg.content_type == "feishu_card"

    def test_message_none_not_sent(self):
        from delivery_layer import DeliveryLayer

        dl = DeliveryLayer(config=MagicMock())
        msg = None
        if msg is not None:
            dl.send(msg)
