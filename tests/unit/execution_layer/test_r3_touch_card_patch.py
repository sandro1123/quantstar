"""R3/D3 触价卡 PATCH 原地更新 — 红绿测试（设计稿-C §2.6, 范式 4）。

核心断言（统筹派单钦点）：「同 card_message_id 原地更新不重发」。
同一张卡「进入区间」→「触发卖出」状态推进走飞书 PUT 原地更新,
找不到原卡/更新失败才降级重发新卡。
"""
import json

import pytest
from unittest.mock import MagicMock

from execution_layer.intraday.touch_card import TouchCardPusher


@pytest.fixture
def pusher(tmp_path, monkeypatch):
    monkeypatch.setenv("QS_TOUCH_CARD_DIR", str(tmp_path))
    p = TouchCardPusher(db=None, stock_pool=None)
    p._delivery = MagicMock()
    p._delivery_ready = True
    return p


def _send_result(remote_id: str = "om_entry_1"):
    r = MagicMock()
    r.status = "success"
    r.remote_message_id = remote_id
    return r


POOL_ITEM = {
    "name": "三一重能", "entry_low": 28.0, "entry_high": 30.0,
    "stop_loss": 26.0, "take_profit": 35.0, "valid_until": "2026-09-05",
    "added_reason": "风电景气",
}


class TestSendCardCapturesRemoteId:
    """_send_card 必须把远端 message_id 落台账 — PATCH 靠它定位原卡。"""

    def test_ledger_records_remote_message_id(self, pusher):
        pusher._delivery.send.return_value = [_send_result("om_entry_1")]
        ok = pusher.push_entry("688349", 29.0, POOL_ITEM)
        assert ok is True
        lines = pusher._ledger_path().read_text(encoding="utf-8").splitlines()
        rec = json.loads(lines[-1])
        assert rec["code"] == "688349"
        assert rec["card_type"] == "entry"
        assert rec["remote_message_id"] == "om_entry_1"

    def test_ledger_empty_id_when_send_returns_nothing(self, pusher):
        pusher._delivery.send.return_value = []
        ok = pusher.push_entry("688349", 29.0, POOL_ITEM)
        assert ok is True
        rec = json.loads(pusher._ledger_path().read_text(encoding="utf-8").splitlines()[-1])
        assert rec["remote_message_id"] == ""


class TestRemoteIdLookup:
    def test_lookup_finds_latest_entry_card(self, pusher):
        pusher._append_ledger({"ts": 1.0, "code": "688349", "card_type": "entry",
                               "remote_message_id": "om_old"})
        pusher._append_ledger({"ts": 2.0, "code": "688349", "card_type": "entry",
                               "remote_message_id": "om_new"})
        assert pusher._remote_id_of("688349", "entry") == "om_new"

    def test_lookup_skips_empty_id_records(self, pusher):
        pusher._append_ledger({"ts": 1.0, "code": "688349", "card_type": "entry",
                               "remote_message_id": "om_old"})
        pusher._append_ledger({"ts": 2.0, "code": "688349", "card_type": "entry",
                               "remote_message_id": ""})
        assert pusher._remote_id_of("688349", "entry") == "om_old"

    def test_lookup_miss_returns_empty(self, pusher):
        assert pusher._remote_id_of("600000", "entry") == ""


class TestStopTriggerPatchInPlace:
    """核心：同 card_message_id 原地更新不重发。"""

    def test_patch_entry_card_not_resend(self, pusher):
        # 前置：当日已推 entry 卡, 台账有远端 id
        pusher._append_ledger({"ts": 1.0, "code": "688349", "card_type": "entry",
                               "remote_message_id": "om_entry_1"})
        pusher._delivery.update.return_value = MagicMock(status="success")

        ok = pusher.push_stop_trigger("688349", 25.5, "跌破参考卖出线", POOL_ITEM)
        assert ok is True
        # 原地更新被调用, 且用的是原卡 id
        pusher._delivery.update.assert_called_once()
        args, _ = pusher._delivery.update.call_args
        assert args[0] == "om_entry_1"
        assert "触发卖出" in json.dumps(args[1], ensure_ascii=False)
        # 不重发新卡
        pusher._delivery.send.assert_not_called()

    def test_patch_success_records_update_ledger(self, pusher):
        pusher._append_ledger({"ts": 1.0, "code": "688349", "card_type": "entry",
                               "remote_message_id": "om_entry_1"})
        pusher._delivery.update.return_value = MagicMock(status="success")
        pusher.push_stop_trigger("688349", 25.5, "跌破参考卖出线", POOL_ITEM)
        recs = [json.loads(l) for l in
                pusher._ledger_path().read_text(encoding="utf-8").splitlines()]
        upd = [r for r in recs if r.get("card_type") == "stop_trigger_update"]
        assert len(upd) == 1
        assert upd[0]["remote_message_id"] == "om_entry_1"


class TestStopTriggerFallback:
    """PATCH 失败/无原卡 → 降级重发新红卡（诚实不静默）。"""

    def test_update_failed_falls_back_to_new_card(self, pusher):
        pusher._append_ledger({"ts": 1.0, "code": "688349", "card_type": "entry",
                               "remote_message_id": "om_entry_1"})
        pusher._delivery.update.return_value = MagicMock(
            status="failed", error_msg="card not found")
        pusher._delivery.send.return_value = [_send_result("om_stop_1")]

        ok = pusher.push_stop_trigger("688349", 25.5, "跌破参考卖出线", POOL_ITEM)
        assert ok is True
        pusher._delivery.update.assert_called_once()
        pusher._delivery.send.assert_called_once()

    def test_no_entry_card_sends_new_red_card(self, pusher):
        pusher._delivery.send.return_value = [_send_result("om_stop_1")]
        ok = pusher.push_stop_trigger("688349", 25.5, "跌破参考卖出线", POOL_ITEM)
        assert ok is True
        pusher._delivery.update.assert_not_called()
        pusher._delivery.send.assert_called_once()

    def test_update_exception_falls_back(self, pusher):
        pusher._append_ledger({"ts": 1.0, "code": "688349", "card_type": "entry",
                               "remote_message_id": "om_entry_1"})
        pusher._delivery.update.side_effect = RuntimeError("boom")
        pusher._delivery.send.return_value = [_send_result("om_stop_1")]
        ok = pusher.push_stop_trigger("688349", 25.5, "跌破参考卖出线", POOL_ITEM)
        assert ok is True
        pusher._delivery.send.assert_called_once()
