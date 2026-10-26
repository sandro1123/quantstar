"""HITL Phase 2: 新 app 通道 + token 缓存键控自测"""
import pytest

from delivery_layer.channels import feishu_dm
from delivery_layer.channels.feishu_hitl_dm import FeishuHitlDMChannel
from delivery_layer.message import Message


class TestTokenCacheKeyedByAppId:
    """D 稿 §0.2 事实错误修正: 原单槽缓存会让新旧 app 互踩 token"""

    def test_two_apps_get_distinct_tokens(self, monkeypatch):
        monkeypatch.setattr(feishu_dm, "_token_cache", {})
        calls = []

        def fake_post(url, json=None, timeout=None):
            calls.append(json["app_id"])

            class R:
                @staticmethod
                def json():
                    return {"code": 0, "tenant_access_token": "tok_" + json["app_id"],
                            "expire": 7200}
            return R

        monkeypatch.setattr(feishu_dm.requests, "post", fake_post)
        t1 = feishu_dm._get_cached_token("app_A", "secret_A")
        t2 = feishu_dm._get_cached_token("app_B", "secret_B")
        assert t1 == "tok_app_A"
        assert t2 == "tok_app_B", "第二个 app 不得覆盖第一个的缓存"
        # 再取 app_A 应命中缓存, 不重新请求
        assert feishu_dm._get_cached_token("app_A", "secret_A") == "tok_app_A"
        assert calls == ["app_A", "app_B"]


class TestHitlDMChannel:
    def _ch(self, monkeypatch, env):
        for k in ("FEISHU_HITL_APP_ID", "FEISHU_HITL_APP_SECRET",
                  "FEISHU_HITL_DM_UNION_ID", "FEISHU_APP_ID", "FEISHU_APP_SECRET",
                  "FEISHU_DM_ID"):
            monkeypatch.delenv(k, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        return FeishuHitlDMChannel(config=None)

    def test_uses_new_app_creds_and_union_id(self, monkeypatch):
        ch = self._ch(monkeypatch, {
            "FEISHU_HITL_APP_ID": "cli_new", "FEISHU_HITL_APP_SECRET": "s_new",
            "FEISHU_HITL_DM_UNION_ID": "on_union",
            "FEISHU_APP_ID": "cli_old", "FEISHU_APP_SECRET": "s_old",
            "FEISHU_DM_ID": "oc_old"})
        assert ch._creds() == ("cli_new", "s_new")
        assert ch._recipient() == ("on_union", "union_id")

    def test_falls_back_to_old_creds_and_chat(self, monkeypatch):
        ch = self._ch(monkeypatch, {
            "FEISHU_APP_ID": "cli_old", "FEISHU_APP_SECRET": "s_old",
            "FEISHU_DM_ID": "oc_old"})
        assert ch._creds() == ("cli_old", "s_old")
        assert ch._recipient() == ("oc_old", "chat_id")

    def test_send_posts_with_union_id_type(self, monkeypatch):
        from delivery_layer.channels import feishu_hitl_dm
        ch = self._ch(monkeypatch, {
            "FEISHU_HITL_APP_ID": "cli_new", "FEISHU_HITL_APP_SECRET": "s_new",
            "FEISHU_HITL_DM_UNION_ID": "on_union"})
        monkeypatch.setattr(feishu_hitl_dm, "_get_cached_token", lambda a, s: "tok")
        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["url"] = url
            captured["payload"] = json

            class R:
                @staticmethod
                def json():
                    return {"code": 0, "data": {"message_id": "om_new"}}
            return R

        monkeypatch.setattr(feishu_hitl_dm.requests, "post", fake_post)
        msg = Message(content={"msg_type": "interactive", "card": {"x": 1}},
                      content_type="execution_alert", title="t")
        result = ch.send(msg)
        assert result.status == "success"
        assert result.remote_message_id == "om_new"
        assert "receive_id_type=union_id" in captured["url"]
        assert captured["payload"]["receive_id"] == "on_union"

    def test_health_requires_creds_and_recipient(self, monkeypatch):
        ch = self._ch(monkeypatch, {})
        assert ch.health() is False
        ch = self._ch(monkeypatch, {
            "FEISHU_HITL_APP_ID": "cli_new", "FEISHU_HITL_APP_SECRET": "s_new",
            "FEISHU_HITL_DM_UNION_ID": "on_union"})
        assert ch.health() is True


class TestHitlCardsRoutedToNewChannel:
    """审批/通知卡必须走 feishu_hitl_dm — 卡归属新 app, 回调才投 HITL daemon"""

    def test_channel_registered(self):
        from delivery_layer import DeliveryLayer
        d = DeliveryLayer()
        assert d.registry.get("feishu_hitl_dm") is not None

    def test_plan_manager_uses_hitl_channel(self):
        import inspect
        from execution_layer.core import plan_manager
        src = inspect.getsource(plan_manager)
        # 两处发卡 (审批卡 + 通知卡) 都应指向新通道
        assert src.count('force_channel="feishu_hitl_dm"') >= 2
