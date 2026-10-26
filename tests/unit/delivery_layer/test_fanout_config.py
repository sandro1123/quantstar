# -*- coding: utf-8 -*-
"""fan_out 配置化验收 — 收口前 executor.FAN_OUT_TYPES 硬编码, 收口后
delivery.fan_out_types 配置为准 (已加载), 缺席回落内置默认 (诚实降级)。

三锚:
  ① 无配置 → 内置默认 (alert_critical 广播 / evening_report 收首成即停)
  ② 配置含 evening_report → 报告类也变广播 (配置驱动生效)
  ③ 配置显式空表 → alert_critical 也不广播 (配置已加载即权威)
"""
import sys
from pathlib import Path

_root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_root / "src"))

from delivery_layer.channels.base import Channel  # noqa: E402
from delivery_layer.executor import DeliveryExecutor  # noqa: E402
from delivery_layer.message import Message  # noqa: E402
from delivery_layer.channels.registry import ChannelRegistry  # noqa: E402
from delivery_layer.router import Router  # noqa: E402


class _StubChannel(Channel):
    def __init__(self, name: str):
        self._name = name
        self.calls = 0

    @property
    def name(self):
        return self._name

    def send(self, msg):
        self.calls += 1
        from delivery_layer.channels.base import DeliveryResult
        return DeliveryResult(message_id=msg.message_id, channel=self._name,
                              status="success")

    def health(self):
        return True


class _CfgStub:
    _loaded = True

    def __init__(self, types):
        self._types = types

    def get(self, key, default=None):
        return self._types if key == "delivery.fan_out_types" else default


def _executor():
    registry = ChannelRegistry()
    chans = [_StubChannel("c1"), _StubChannel("c2")]
    for c in chans:
        registry.register(c)
        registry.mark_healthy(c.name)
    ex = DeliveryExecutor(registry, Router())
    return ex, chans


def _send(ex, content_type):
    from unittest.mock import patch
    msg = Message(content={"body": "x"}, content_type=content_type, title="t")
    with patch.object(ex.router, "resolve", return_value=["c1", "c2"]):
        ex.send(msg, report_mode="production")


def test_default_fallback_broadcasts_alert_only():
    ex, (c1, c2) = _executor()
    _send(ex, "alert_critical")
    assert c1.calls == 1 and c2.calls == 1, "默认面: 告警须广播"
    ex2, (d1, d2) = _executor()
    _send(ex2, "evening_report")
    assert d1.calls == 1 and d2.calls == 0, "默认面: 报告收首成即停"


def test_config_driven_fanout(monkeypatch):
    monkeypatch.setattr("config_layer.get_config",
                        lambda: _CfgStub(["evening_report"]))
    ex, (c1, c2) = _executor()
    _send(ex, "evening_report")
    assert c1.calls == 1 and c2.calls == 1, "配置含 evening_report → 广播"


def test_config_loaded_is_authoritative(monkeypatch):
    monkeypatch.setattr("config_layer.get_config", lambda: _CfgStub([]))
    ex, (c1, c2) = _executor()
    _send(ex, "alert_critical")
    assert c1.calls == 1 and c2.calls == 0, "配置空表 → alert 也收首成即停"
