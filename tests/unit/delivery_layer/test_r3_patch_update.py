"""R3/D3 delivery_layer 原地更新能力 — 红绿测试（范式 4）。

链路：DeliveryLayer.update → DeliveryExecutor.update → channel.patch
→ 飞书 PUT /im/v1/messages/{message_id}。
"""
import json

import pytest
from unittest.mock import MagicMock, patch

from delivery_layer.channels.base import DeliveryResult
from delivery_layer.channels.feishu_dm import FeishuDMChannel, _patch_via_api
from delivery_layer.channels.registry import ChannelRegistry
from delivery_layer.executor import DeliveryExecutor
from delivery_layer.router import Router


class _FakePatchChannel:
    name = "feishu_dm"

    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    def patch(self, message_id, content):
        self.calls.append((message_id, content))
        return DeliveryResult(
            message_id=message_id, channel=self.name,
            status="success" if self.ok else "failed",
            error_msg="" if self.ok else "api error")

    def health(self):
        return True


class TestExecutorUpdate:
    def _exec(self, channel):
        reg = ChannelRegistry()
        reg.register(channel)
        return DeliveryExecutor(reg, Router())

    def test_update_routes_to_channel_patch(self):
        ch = _FakePatchChannel(ok=True)
        ex = self._exec(ch)
        result = ex.update("om_1", {"msg_type": "interactive", "card": {}})
        assert result.status == "success"
        assert ch.calls[0][0] == "om_1"

    def test_update_failed_channel_returns_failed(self):
        ch = _FakePatchChannel(ok=False)
        ex = self._exec(ch)
        result = ex.update("om_1", {"card": {}})
        assert result.status == "failed"

    def test_update_channel_without_patch_capability(self):
        class NoPatch:
            name = "feishu_dm"

            def health(self):
                return True
        ex = self._exec(NoPatch())
        result = ex.update("om_1", {"card": {}})
        assert result.status == "failed"
        assert "无原地更新能力" in (result.error_msg or "")

    def test_update_unknown_channel(self):
        ex = self._exec(_FakePatchChannel())
        result = ex.update("om_1", {"card": {}}, channel_name="no_such")
        assert result.status == "failed"

    def test_update_exception_returns_failed_not_raise(self):
        ch = _FakePatchChannel()
        ch.patch = MagicMock(side_effect=RuntimeError("boom"))
        ex = self._exec(ch)
        result = ex.update("om_1", {"card": {}})
        assert result.status == "failed"


class TestDeliveryLayerUpdatePassthrough:
    def test_layer_update_delegates_to_executor(self):
        from delivery_layer import DeliveryLayer
        layer = DeliveryLayer.__new__(DeliveryLayer)
        layer.executor = MagicMock()
        layer.executor.update.return_value = MagicMock(status="success")
        result = layer.update("om_1", {"card": {}})
        # DeliveryLayer.update 以位置参数透传 channel_name
        layer.executor.update.assert_called_once_with("om_1", {"card": {}}, "feishu_dm")
        assert result.status == "success"


class TestFeishuDmChannelPatch:
    def test_patch_without_credentials_fails(self, monkeypatch):
        monkeypatch.delenv("FEISHU_APP_ID", raising=False)
        monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
        ch = FeishuDMChannel(config=None)
        result = ch.patch("om_1", {"card": {}})
        assert result.status == "failed"

    def test_patch_empty_message_id_fails(self, monkeypatch):
        monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
        monkeypatch.setenv("FEISHU_APP_SECRET", "s")
        ch = FeishuDMChannel(config=None)
        result = ch.patch("", {"card": {}})
        assert result.status == "failed"


class TestPatchViaApi:
    def test_put_success(self):
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"code": 0}
        with patch("delivery_layer.channels.feishu_dm.requests.put",
                   return_value=fake_resp) as mock_put:
            ok, err = _patch_via_api("om_1", {
                "msg_type": "interactive",
                "card": {"header": {"template": "red"}},
            }, "t-token")
        assert ok is True and err == ""
        # PUT 打到 /im/v1/messages/{message_id}, body 带 msg_type+content
        url = mock_put.call_args.kwargs.get("url") or mock_put.call_args.args[0]
        assert url.endswith("/im/v1/messages/om_1")
        payload = mock_put.call_args.kwargs["json"]
        assert payload["msg_type"] == "interactive"
        assert "header" in json.loads(payload["content"])

    def test_put_api_error(self):
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"code": 230001, "msg": "card not found"}
        with patch("delivery_layer.channels.feishu_dm.requests.put",
                   return_value=fake_resp):
            ok, err = _patch_via_api("om_1", {"card": {}}, "t-token")
        assert ok is False
        assert "card not found" in err
