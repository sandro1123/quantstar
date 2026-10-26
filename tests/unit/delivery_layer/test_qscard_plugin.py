"""qscard:// Apprise 插件红绿测试 (P0-2).

红: 未知 target / 缺 env / token 失败 / push 失败 → False 不抛异常。
绿: scheme 注册 / Apprise 分发 / 卡头四色映射 / 卡片内容 / dm+group 双 target。
全部网络调用 monkeypatch, 零真实发送; env autouse 隔离防插件自载真 .env。
"""
import pytest

apprise = pytest.importorskip("apprise")

from delivery_layer.apprise_plugins import qscard_plugin as qp
from delivery_layer.apprise_plugins.qscard_plugin import (
    HEADER_COLOR,
    build_card,
    send_qscard,
)

ENV_KEYS = ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_DM_ID", "FEISHU_GROUP_ID")


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """清 env + 阻断插件自载 .env (worktree 软链指向生产 .env) + 清 token 缓存."""
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(qp._load_env_once, "_done", True, raising=False)
    qp._token_cache.clear()
    yield
    qp._token_cache.clear()


@pytest.fixture
def armed(monkeypatch):
    """齐 env + mock token/push, 返回 push 调用记录 list."""
    monkeypatch.setenv("FEISHU_APP_ID", "id-test")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret-test")
    monkeypatch.setenv("FEISHU_DM_ID", "oc_dm-test")
    monkeypatch.setenv("FEISHU_GROUP_ID", "oc-group-test")
    monkeypatch.setattr(qp, "_get_token", lambda app_id, secret: "tok-test")
    calls = []

    def fake_push(chat_id, card, token):
        calls.append({"chat_id": chat_id, "card": card, "token": token})
        return True

    monkeypatch.setattr(qp, "_push_card", fake_push)
    return calls


class TestRegistration:
    def test_scheme_registered_in_manager(self):
        from apprise.plugins import N_MGR
        assert "qscard" in N_MGR

    def test_apprise_accepts_qscard_url(self):
        a = apprise.Apprise()
        assert a.add("qscard://dm") is True
        assert a.add("qscard://group") is True


class TestRed:
    def test_unknown_target_returns_false(self, armed):
        assert send_qscard("bogus", "t", "b") is False
        assert armed == []  # 未触达 push

    def test_missing_env_returns_false(self):
        assert send_qscard("dm", "t", "b") is False

    def test_missing_chat_id_returns_false(self, monkeypatch):
        monkeypatch.setenv("FEISHU_APP_ID", "id-test")
        monkeypatch.setenv("FEISHU_APP_SECRET", "secret-test")
        assert send_qscard("dm", "t", "b") is False

    def test_token_failure_returns_false(self, monkeypatch):
        monkeypatch.setenv("FEISHU_APP_ID", "id-test")
        monkeypatch.setenv("FEISHU_APP_SECRET", "secret-test")
        monkeypatch.setenv("FEISHU_DM_ID", "oc-dm-test")
        monkeypatch.setattr(qp, "_get_token", lambda app_id, secret: None)
        assert send_qscard("dm", "t", "b") is False

    def test_push_failure_returns_false(self, armed, monkeypatch):
        monkeypatch.setattr(qp, "_push_card", lambda chat_id, card, token: False)
        assert send_qscard("dm", "t", "b") is False

    def test_apprise_notify_false_on_failure(self, monkeypatch):
        monkeypatch.setattr(qp, "_get_token", lambda app_id, secret: None)
        a = apprise.Apprise()
        a.add("qscard://dm")
        assert bool(a.notify(title="t", body="b")) is False


class TestGreen:
    def test_send_dm_success(self, armed):
        assert send_qscard("dm", "晨报", "今日 3 信号", "success") is True
        assert len(armed) == 1
        call = armed[0]
        assert call["chat_id"] == "oc_dm-test"
        assert call["token"] == "tok-test"
        assert call["card"]["header"]["template"] == "green"
        assert call["card"]["header"]["title"]["content"] == "晨报"
        assert call["card"]["elements"][0]["content"] == "今日 3 信号"
        assert call["card"]["elements"][0]["tag"] == "markdown"

    def test_send_group_success(self, armed):
        assert send_qscard("group", "告警", "内容", "failure") is True
        assert armed[0]["chat_id"] == "oc-group-test"
        assert armed[0]["card"]["header"]["template"] == "red"

    def test_apprise_notify_dispatches_to_dm(self, armed):
        a = apprise.Apprise()
        a.add("qscard://dm")
        assert bool(a.notify(title="T", body="B", notify_type=apprise.NotifyType.WARNING)) is True
        assert len(armed) == 1
        assert armed[0]["chat_id"] == "oc_dm-test"
        assert armed[0]["card"]["header"]["template"] == "orange"

    def test_apprise_notify_dispatches_to_group(self, armed):
        a = apprise.Apprise()
        a.add("qscard://group")
        assert bool(a.notify(title="T", body="B", notify_type=apprise.NotifyType.SUCCESS)) is True
        assert armed[0]["chat_id"] == "oc-group-test"
        assert armed[0]["card"]["header"]["template"] == "green"

    def test_apprise_default_notify_type_is_info(self, armed):
        a = apprise.Apprise()
        a.add("qscard://dm")
        assert bool(a.notify(body="B")) is True
        assert armed[0]["card"]["header"]["template"] == "blue"

    def test_header_color_map_covers_four_types(self):
        assert HEADER_COLOR == {
            "info": "blue",
            "success": "green",
            "warning": "orange",
            "failure": "red",
        }

    def test_unknown_notify_type_falls_back_blue(self):
        assert build_card("t", "b", "weird-type")["header"]["template"] == "blue"

    def test_empty_title_defaults_to_quantstar(self):
        assert build_card("", "b")["header"]["title"]["content"] == "QuantStar"
