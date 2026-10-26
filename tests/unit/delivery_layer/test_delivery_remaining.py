"""Tests for delivery layer remaining modules"""
from delivery_layer.router import Router
from delivery_layer.channels.registry import ChannelRegistry
from delivery_layer.message import Message


class TestRouter:
    def test_resolve_default(self):
        r = Router()
        msg = Message(content={"text": "hello"}, content_type="test", title="test")
        channels = r.resolve(msg)
        assert isinstance(channels, list)

    def test_resolve_quiet_mode(self):
        r = Router()
        msg = Message(content={"text": "hello"}, content_type="test", title="test", loudness="off")
        channels = r.resolve(msg)
        assert len(channels) == 0


class TestChannelRegistry:
    def test_register_and_get(self):
        reg = ChannelRegistry()
        from delivery_layer.channels.base import Channel, DeliveryResult
        class TestChannel(Channel):
            @property
            def name(self): return "test"
            def send(self, msg): return DeliveryResult(message_id="", channel="test", status="success")
            def health(self): return True
        reg.register(TestChannel())
        ch = reg.get("test")
        assert ch is not None
        assert ch.name == "test"

    def test_healthy_count(self):
        reg = ChannelRegistry()
        from delivery_layer.channels.base import Channel, DeliveryResult
        class HealthyChannel(Channel):
            @property
            def name(self): return "healthy"
            def send(self, msg): return DeliveryResult(message_id="", channel="healthy", status="success")
            def health(self): return True
        class UnhealthyChannel(Channel):
            @property
            def name(self): return "unhealthy"
            def send(self, msg): return DeliveryResult(message_id="", channel="unhealthy", status="failed")
            def health(self): return False
        reg.register(HealthyChannel())
        reg.register(UnhealthyChannel())
        assert reg.healthy_count() == 1

    def test_get_healthy(self):
        reg = ChannelRegistry()
        from delivery_layer.channels.base import Channel, DeliveryResult
        class TestChannel(Channel):
            @property
            def name(self): return "test"
            def send(self, msg): return DeliveryResult(message_id="", channel="test", status="success")
            def health(self): return True
        reg.register(TestChannel())
        healthy = reg.get_healthy(["test", "nonexistent"])
        assert len(healthy) == 1


class TestMessage:
    def test_message_defaults(self):
        msg = Message(content={"key": "val"}, content_type="test", title="t")
        assert msg.loudness == "on"
        assert msg.message_id is not None

    def test_message_quiet(self):
        msg = Message(content={}, content_type="test", title="t", loudness="off")
        assert msg.loudness == "off"


class TestDeliveryHealth:
    def _make_registry(self):
        from delivery_layer.channels.registry import ChannelRegistry
        from delivery_layer.channels.base import Channel, DeliveryResult
        reg = ChannelRegistry()

        class BoolHealthy(Channel):
            @property
            def name(self): return "bool_ok"
            def send(self, msg): return DeliveryResult(message_id="", channel="bool_ok", status="success")
            def health(self): return True

        class BoolUnhealthy(Channel):
            @property
            def name(self): return "bool_bad"
            def send(self, msg): return DeliveryResult(message_id="", channel="bool_bad", status="failed")
            def health(self): return False

        class DictHealthy(Channel):
            @property
            def name(self): return "dict_ok"
            def send(self, msg): return DeliveryResult(message_id="", channel="dict_ok", status="success")
            def health(self): return {"status": "pass", "detail": "可达"}

        reg.register(BoolHealthy())
        reg.register(BoolUnhealthy())
        reg.register(DictHealthy())
        return reg

    def test_health_probe_iterates_get_all(self):
        from delivery_layer import DeliveryLayer
        dl = DeliveryLayer.__new__(DeliveryLayer)
        dl.config = None
        dl.registry = self._make_registry()
        from unittest.mock import MagicMock
        dl.router = MagicMock()
        dl.executor = MagicMock()

        h = dl.health()
        assert h.status == "yellow"
        names = {c.name for c in h.checks}
        assert "channels_count" in names
        assert "channel_bool_ok" in names
        assert "channel_dict_ok" in names

    def test_health_reflects_unhealthy_channel(self):
        from delivery_layer import DeliveryLayer
        dl = DeliveryLayer.__new__(DeliveryLayer)
        dl.config = None
        dl.registry = self._make_registry()
        from unittest.mock import MagicMock
        dl.router = MagicMock()
        dl.executor = MagicMock()

        h = dl.health()
        bool_bad = next(c for c in h.checks if c.name == "channel_bool_bad")
        ok_checks = [c for c in h.checks if c.name.startswith("channel_") and c.status == "pass"]
        assert bool_bad.status == "warn"
        assert len(ok_checks) >= 2

    def test_health_empty_registry(self):
        from delivery_layer import DeliveryLayer
        from delivery_layer.channels.registry import ChannelRegistry
        from unittest.mock import MagicMock
        dl = DeliveryLayer.__new__(DeliveryLayer)
        dl.config = None
        dl.registry = ChannelRegistry()
        dl.router = MagicMock()
        dl.executor = MagicMock()

        h = dl.health()
        assert h.status == "green"