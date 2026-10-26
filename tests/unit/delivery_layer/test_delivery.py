"""Delivery Layer 单元测试"""
import pytest
from unittest.mock import MagicMock
from delivery_layer import DeliveryLayer
from delivery_layer.message import Message
from delivery_layer.executor import RateLimiter


@pytest.fixture
def delivery_layer():
    return DeliveryLayer()


class TestDeliveryLayerInit:
    def test_creates_with_defaults(self, delivery_layer):
        assert delivery_layer.config is None
        assert delivery_layer.registry is not None
        assert delivery_layer.router is not None

    def test_creates_with_config(self):
        config = MagicMock()
        dl = DeliveryLayer(config=config)
        assert dl.config is config


class TestDeliveryLayerSend:
    def test_send_with_loudness_off(self, delivery_layer):
        msg = Message(content={"text": "test"}, content_type="test", title="Test", loudness="off")
        result = delivery_layer.send(msg)
        assert result == []

    def test_send_returns_results(self, delivery_layer):
        msg = Message(content={"text": "test"}, content_type="test", title="Test")
        result = delivery_layer.send(msg)
        assert isinstance(result, list)


class TestDeliveryLayerSendAsync:
    def test_send_async_returns_future(self, delivery_layer):
        msg = Message(content={"text": "test"}, content_type="test", title="Test")
        future = delivery_layer.send_async(msg)
        assert hasattr(future, 'result')


class TestDeliveryLayerHealth:
    def test_health_returns_layer_health(self, delivery_layer):
        health = delivery_layer.health()
        assert health.layer == "delivery"
        assert health.status in ("green", "yellow", "red")


class TestDeliveryLayerShutdown:
    def test_shutdown_returns_true(self, delivery_layer):
        assert delivery_layer.shutdown() is True


class TestRateLimiter:
    def test_can_send_first_time(self):
        rl = RateLimiter()
        assert rl.can_send("morning_brief") is True

    def test_cooldown_blocks(self):
        rl = RateLimiter()
        rl.can_send("morning_brief")
        assert rl.can_send("morning_brief") is False

    def test_alert_critical_always_sends(self):
        rl = RateLimiter()
        rl.can_send("alert_critical")
        assert rl.can_send("alert_critical") is True


class TestDeliveryLayerDecorators:
    def test_send_has_catch_decorator(self):
        import delivery_layer
        method = delivery_layer.DeliveryLayer.send
        assert hasattr(method, '__wrapped__')

    def test_send_async_has_catch_decorator(self):
        import delivery_layer
        method = delivery_layer.DeliveryLayer.send_async
        assert hasattr(method, '__wrapped__')
