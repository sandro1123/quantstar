"""HITL Phase 1: 飞书审批卡回调闭环自测 (mock 回调, 不打真飞书)

真端到端 (人在飞书里点按钮) 需先在开放平台开「长连接模式」+ 卡片回调权限,
那一步另行调度。本测试把合成的 card.action.trigger 事件喂进**真实的**
EventDispatcherHandler, 因此顺带验证了 Phase 1 的架构地基:

    lark-oapi 1.7.3 的 ws/client.py 对 MessageType.CARD 帧直接 return (丢弃),
    卡片回调只能以 EVENT 帧经 "p2.card.action.trigger" 键派发。

若哪天升级 SDK 改了派发键, 这里的 test_dispatcher_routes_* 会先红, 不会等到
线上点按钮没反应才发现。
"""
import contextlib
import json

import pytest

from common.database import Database
from delivery_layer import hitl_ws_daemon as daemon
from execution_layer.core.human_in_loop import HumanInLoop


def _event(action, review_id, user_id="sandro", message_id="om_original_card"):
    """合成一条飞书卡片按钮回调 (v2 事件体)"""
    return json.dumps({
        "schema": "2.0",
        "header": {"event_type": "card.action.trigger", "token": "tk",
                   "event_id": "ev1", "app_id": "cli_test"},
        "event": {
            "operator": {"open_id": "ou_abc", "user_id": user_id},
            "action": {"tag": "button",
                       "value": {"action": action, "review_id": str(review_id)}},
            "context": {"open_message_id": message_id, "open_chat_id": "oc_1"},
        },
    }).encode("utf-8")


@pytest.fixture
def hitl(tmp_path, monkeypatch):
    """内存库 + 出站网络全打桩"""
    h = HumanInLoop(Database(":memory:"))

    @contextlib.contextmanager
    def _session():
        yield h

    monkeypatch.setattr(daemon, "hitl_session", _session)
    monkeypatch.setattr(daemon, "HEARTBEAT_PATH", str(tmp_path / "hb.jsonl"))

    calls = {"patch": [], "ledger": []}
    monkeypatch.setattr(daemon, "patch_card",
                        lambda mid, card: calls["patch"].append((mid, card)) or True)
    monkeypatch.setattr(daemon, "write_bitable_ledger",
                        lambda *a, **k: calls["ledger"].append(a) or True)
    h.calls = calls
    h.hb_path = str(tmp_path / "hb.jsonl")
    return h


def _seed(h, code="000001", decision_type="buy"):
    verdict = h.evaluate(code, decision_type, "放量突破+MACD金叉", 82.5, 0.62,
                         entry_low=12.5, entry_high=13.0,
                         stop_loss=11.8, take_profit=14.5)
    assert verdict.action == "require_review"
    return verdict.review_id


def _heartbeat_events(path):
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(line)["event"] for line in f if line.strip()]
    except FileNotFoundError:
        return []


class TestEvaluateReturnsReviewId:
    """发卡需要 review_id, 而 evaluate 原先只回 action/reason"""

    def test_review_id_matches_queue_row(self, hitl):
        review_id = _seed(hitl)
        assert review_id > 0
        assert hitl.get_pending()[0]["id"] == review_id

    def test_auto_approve_carries_no_review_id(self, hitl):
        verdict = hitl.evaluate("000002", "buy", "高置信", 90, 0.9)
        assert verdict.action == "auto_approve"
        assert verdict.review_id == 0

    def test_set_message_id_roundtrip(self, hitl):
        review_id = _seed(hitl)
        assert hitl.set_message_id(review_id, "om_xyz") is True
        assert hitl.get(review_id)["message_id"] == "om_xyz"

    def test_set_message_id_rejects_empty(self, hitl):
        review_id = _seed(hitl)
        assert hitl.set_message_id(review_id, "") is False


class TestDispatcherRouting:
    """真实 dispatcher 派发: 证明 p2.card.action.trigger 键挂得上"""

    def test_handler_registers_card_action_key(self):
        handler = daemon.build_handler()
        assert "p2.card.action.trigger" in handler._callback_processor_map

    def test_dispatcher_routes_approve(self, hitl):
        review_id = _seed(hitl)
        resp = daemon.build_handler()._do_without_validation(_event("approve", review_id))
        row = hitl.get(review_id)
        assert row["status"] == "approved"
        assert row["reviewer"] == "sandro"
        assert resp is not None and resp.toast.type == "success"

    def test_dispatcher_routes_reject(self, hitl):
        review_id = _seed(hitl)
        resp = daemon.build_handler()._do_without_validation(_event("reject", review_id))
        assert hitl.get(review_id)["status"] == "rejected"
        assert resp.toast.type == "success"

    def test_operator_falls_back_to_open_id(self, hitl):
        review_id = _seed(hitl)
        daemon.build_handler()._do_without_validation(
            _event("approve", review_id, user_id=""))
        assert hitl.get(review_id)["reviewer"] == "ou_abc"


class TestIdempotency:
    """双通道/重复点击靠 UPDATE ... WHERE status='pending' 收口"""

    def test_double_click_does_not_reprocess(self, hitl):
        review_id = _seed(hitl)
        handler = daemon.build_handler()
        handler._do_without_validation(_event("approve", review_id))
        resp2 = handler._do_without_validation(_event("approve", review_id, user_id="someone_else"))
        row = hitl.get(review_id)
        assert row["status"] == "approved"
        assert row["reviewer"] == "sandro", "第二次点击不得改写审批人"
        assert resp2.toast.type == "info"

    def test_reject_after_approve_is_noop(self, hitl):
        review_id = _seed(hitl)
        handler = daemon.build_handler()
        handler._do_without_validation(_event("approve", review_id))
        handler._do_without_validation(_event("reject", review_id))
        assert hitl.get(review_id)["status"] == "approved"

    def test_second_click_writes_no_extra_ledger_row(self, hitl):
        review_id = _seed(hitl)
        handler = daemon.build_handler()
        handler._do_without_validation(_event("approve", review_id))
        handler._do_without_validation(_event("approve", review_id))
        assert len(hitl.calls["ledger"]) == 1


class TestBadInput:
    def test_unknown_review_id(self, hitl):
        resp = daemon.build_handler()._do_without_validation(_event("approve", 99999))
        assert resp.toast.type == "error"
        assert not hitl.calls["ledger"]

    def test_unknown_action_verb(self, hitl):
        review_id = _seed(hitl)
        resp = daemon.build_handler()._do_without_validation(_event("delete_everything", review_id))
        assert resp.toast.type == "error"
        assert hitl.get(review_id)["status"] == "pending", "未知动作不得改状态"

    def test_non_numeric_review_id(self, hitl):
        _seed(hitl)
        resp = daemon.build_handler()._do_without_validation(_event("approve", "not-a-number"))
        assert resp.toast.type == "error"

    def test_missing_review_id(self, hitl):
        payload = json.dumps({
            "schema": "2.0",
            "header": {"event_type": "card.action.trigger", "token": "tk"},
            "event": {"operator": {"open_id": "ou_abc"},
                      "action": {"tag": "button", "value": {"action": "approve"}},
                      "context": {"open_message_id": "om_1"}},
        }).encode("utf-8")
        resp = daemon.build_handler()._do_without_validation(payload)
        assert resp.toast.type == "error"


class TestCardAndLedger:
    def test_patch_targets_callback_message_id(self, hitl):
        """回调自带 open_message_id — 即使库里没回填 message_id 也能 PATCH"""
        review_id = _seed(hitl)
        assert hitl.get(review_id)["message_id"] is None
        daemon.build_handler()._do_without_validation(
            _event("approve", review_id, message_id="om_from_callback"))
        assert hitl.calls["patch"][0][0] == "om_from_callback"

    def test_result_card_replaces_buttons(self, hitl):
        review_id = _seed(hitl)
        daemon.build_handler()._do_without_validation(_event("approve", review_id))
        _, card = hitl.calls["patch"][0]
        assert card["header"]["template"] == "green"
        assert "已批准" in card["header"]["title"]["content"]
        tags = [e.get("tag") for e in card["elements"]]
        assert "action" not in tags, "结果卡不得再带按钮"

    def test_ledger_row_shape(self, hitl):
        review_id = _seed(hitl)
        daemon.build_handler()._do_without_validation(_event("approve", review_id))
        args = hitl.calls["ledger"][0]
        # (review_id, code, decision_type, action, reviewer, note, channel, signal_summary)
        assert args[0] == review_id
        assert args[1] == "000001"
        assert args[3] == "approve"
        assert args[6] == "ws_callback"
        assert "confidence=0.62" in args[7]


class TestObservability:
    def test_callback_is_logged_to_heartbeat(self, hitl):
        review_id = _seed(hitl)
        daemon.build_handler()._do_without_validation(_event("approve", review_id))
        assert "callback" in _heartbeat_events(hitl.hb_path)

    def test_noop_click_is_logged(self, hitl):
        review_id = _seed(hitl)
        handler = daemon.build_handler()
        handler._do_without_validation(_event("approve", review_id))
        handler._do_without_validation(_event("approve", review_id))
        assert "callback_noop" in _heartbeat_events(hitl.hb_path)

    def test_heartbeat_rotation_keeps_three_generations(self, hitl, monkeypatch):
        monkeypatch.setattr(daemon, "HEARTBEAT_MAX_BYTES", 1)
        for _ in range(5):
            daemon.write_heartbeat("probe")
        import os
        assert os.path.exists(hitl.hb_path + ".1")
        assert not os.path.exists(hitl.hb_path + ".4"), "只保留 3 代"


class TestApprovalCardTemplate:
    def test_card_has_six_fields_and_two_buttons(self):
        from delivery_layer.hitl_card_templates import build_approval_card
        card = build_approval_card(42, "000001", "buy", "放量突破", 82.5, 0.62,
                                  12.5, 13.0, 11.8, 14.5, name="平安银行")
        assert len(card["elements"][0]["fields"]) == 6
        actions = [e for e in card["elements"] if e.get("tag") == "action"][0]["actions"]
        values = [a["value"] for a in actions]
        assert values == [{"action": "approve", "review_id": "42"},
                          {"action": "reject", "review_id": "42"}]


class TestApproverHardening:
    """审批人白名单 / 卡片一致性 / 过期"""

    def test_allowlist_rejects_unknown_operator(self, hitl, monkeypatch):
        monkeypatch.setenv("HITL_APPROVER_OPEN_IDS", "ou_owner")
        review_id = _seed(hitl)
        resp = daemon.build_handler()._do_without_validation(_event("approve", review_id))
        assert hitl.get(review_id)["status"] == "pending"
        assert resp.toast.type == "error"

    def test_allowlist_accepts_listed_operator(self, hitl, monkeypatch):
        monkeypatch.setenv("HITL_APPROVER_OPEN_IDS", "ou_other, ou_abc")
        review_id = _seed(hitl)
        daemon.build_handler()._do_without_validation(_event("approve", review_id))
        assert hitl.get(review_id)["status"] == "approved"

    def test_mismatched_card_message_rejected(self, hitl):
        review_id = _seed(hitl)
        hitl.set_message_id(review_id, "om_real_card")
        resp = daemon.build_handler()._do_without_validation(
            _event("approve", review_id, message_id="om_forwarded_copy"))
        assert hitl.get(review_id)["status"] == "pending"
        assert resp.toast.type == "error"

    def test_expired_review_cannot_be_approved(self, hitl, monkeypatch):
        review_id = _seed(hitl)
        import datetime as _dt
        old_ts = (_dt.datetime.now() - _dt.timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
        conn = hitl.db.get_connection() if hasattr(hitl.db, "get_connection") else hitl.db
        conn.execute("UPDATE human_review_queue SET created_at = ? WHERE id = ?", (old_ts, review_id))
        monkeypatch.setenv("HITL_REVIEW_TTL_MIN", "60")
        resp = daemon.build_handler()._do_without_validation(_event("approve", review_id))
        assert hitl.get(review_id)["status"] == "pending"
        assert resp.toast.type == "error"
