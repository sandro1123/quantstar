"""S0 回归测试 — delivery 层止血修复 (2026-08-16)

覆盖审计 P0: feishu_group 失败 webhook(19001) 假成功 + 路由无降级。
"""
from unittest.mock import patch

from delivery_layer.channels.base import Channel, DeliveryResult
from delivery_layer.channels.feishu_group import FeishuGroupChannel
from delivery_layer.channels.registry import ChannelRegistry
from delivery_layer.executor import DeliveryExecutor
from delivery_layer.message import Message
from delivery_layer.router import Router


class _Resp:
    def __init__(self, payload: dict):
        self._payload = payload
        self.content = b"x"

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _StubChannel(Channel):
    def __init__(self, name: str, status: str = "success"):
        self._name = name
        self._status = status
        self.calls = 0

    @property
    def name(self):
        return self._name

    def send(self, msg):
        self.calls += 1
        return DeliveryResult(message_id=msg.message_id, channel=self._name, status=self._status)

    def health(self):
        return True


class TestFeishuGroupErrorCode:
    def _send(self, payload):
        ch = FeishuGroupChannel(config=None)
        msg = Message(content={"msg_type": "text", "content": {"text": "t"}},
                      content_type="test", title="t")
        with patch.dict("os.environ", {"FEISHU_GROUP_WEBHOOK": "https://example/hook"}, clear=False):
            with patch("delivery_layer.channels.feishu_group.requests.post", return_value=_Resp(payload)):
                return ch.send(msg)

    def test_code_19001_reports_failure(self):
        result = self._send({"code": 19001, "msg": "invalid webhook"})
        assert result.status == "failed"
        assert "19001" in (result.error_msg or "")

    def test_code_0_reports_success(self):
        result = self._send({"code": 0, "msg": "ok"})
        assert result.status == "success"

    def test_missing_code_treated_as_success(self):
        result = self._send({})
        assert result.status == "success"


class TestDeliveryExecutorFallback:
    def _executor(self, channels):
        registry = ChannelRegistry()
        for c in channels:
            registry.register(c)
            registry.mark_healthy(c.name)
        return DeliveryExecutor(registry, Router())

    def test_stops_after_first_success(self):
        first = _StubChannel("c1", "success")
        second = _StubChannel("c2", "success")
        ex = self._executor([first, second])
        msg = Message(content={"body": "a"}, content_type="morning_brief", title="t")
        with patch.object(ex.router, "resolve", return_value=["c1", "c2"]):
            results = ex.send(msg, report_mode="production")
        assert first.calls == 1
        assert second.calls == 0
        assert len(results) == 1

    def test_falls_back_on_failure(self):
        first = _StubChannel("c1", "failed")
        second = _StubChannel("c2", "success")
        ex = self._executor([first, second])
        msg = Message(content={"body": "b"}, content_type="evening_report", title="t")
        with patch.object(ex.router, "resolve", return_value=["c1", "c2"]):
            results = ex.send(msg, report_mode="production")
        assert first.calls == 1 and second.calls == 1
        assert results[-1].status == "success"

    def test_fan_out_sends_all(self):
        first = _StubChannel("c1", "success")
        second = _StubChannel("c2", "success")
        ex = self._executor([first, second])
        msg = Message(content={"body": "c"}, content_type="alert_critical", title="t")
        with patch.object(ex.router, "resolve", return_value=["c1", "c2"]):
            results = ex.send(msg, report_mode="production")
        assert first.calls == 1 and second.calls == 1
        assert len(results) == 2
