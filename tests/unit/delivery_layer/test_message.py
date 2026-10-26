"""Delivery Layer 单元测试"""
from delivery_layer.message import Message


def test_message_creation():
    m = Message(content={"key": "val"}, content_type="test", title="测试")
    assert m.content_type == "test"
    assert m.title == "测试"
    assert m.severity == "info"
    assert m.loudness == "on"
    assert len(m.message_id) == 12


def test_message_defaults():
    m = Message(content={}, content_type="alert", title="告警", severity="critical")
    assert m.severity == "critical"


def test_channel_registry():
    from delivery_layer.channels.registry import ChannelRegistry
    from delivery_layer.channels.feishu_dm import FeishuDMChannel
    reg = ChannelRegistry()
    ch = FeishuDMChannel()
    reg.register(ch)
    assert reg.get("feishu_dm") is ch