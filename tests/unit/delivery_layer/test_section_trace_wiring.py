"""B席 ⑤ 接线测试: executor.send 成功 → 落段留痕; 失败 → 不落。"""
import pytest
from common.database import Database
from delivery_layer.channels.base import Channel, DeliveryResult
from delivery_layer.channels.registry import ChannelRegistry
from delivery_layer.executor import DeliveryExecutor
from delivery_layer.log_store import DeliveryLogStore
from delivery_layer.message import Message
from delivery_layer.router import Router


class FakeChannel(Channel):
    def __init__(self, name, ok=True):
        self._name = name
        self._ok = ok
    @property
    def name(self):
        return self._name
    def health(self):
        return True
    def send(self, message):
        return DeliveryResult(message_id=message.message_id, channel=self.name,
                              status="success" if self._ok else "failed",
                              error_msg="" if self._ok else "boom")


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "t.db")); d.init_db(); return d


def _exec(db, ok=True):
    reg = ChannelRegistry(); reg.register(FakeChannel("feishu_group", ok))
    return DeliveryExecutor(registry=reg, router=Router(), db=db)


def _mb_msg():
    return Message(content={"sections": [
        {"name": "ActionCandidatesSection", "title": "🎯", "body": "abc"},
        {"name": "ConclusionSection", "title": "", "body": ""},
    ], "date": "2026-09-16"},
        content_type="morning_brief", title="t", force_channel="feishu_group")


def test_send_success_records_sections(db):
    ex = _exec(db, ok=True)
    ex.send(_mb_msg())
    rows = DeliveryLogStore(db).section_trace("2026-09-16", "morning_brief")
    assert len(rows) == 2
    assert rows[1]["is_empty"] == 1  # 空段显式标记


def test_send_failure_does_not_record_sections(db):
    ex = _exec(db, ok=False)
    ex.send(_mb_msg())
    rows = DeliveryLogStore(db).section_trace("2026-09-16", "morning_brief")
    assert rows == []


def test_non_report_message_no_section_trace(db):
    """非报告消息 (无 sections) 不落段留痕 (零回归)"""
    ex = _exec(db, ok=True)
    ex.send(Message(content={"msg_type": "text"}, content_type="intraday_alert",
                    title="t", force_channel="feishu_group"))
    assert DeliveryLogStore(db).section_trace("2026-09-16") == []
