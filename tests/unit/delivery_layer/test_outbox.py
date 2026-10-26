"""outbox 落地仓 + 晨报水位 红绿测试 (P0-2 failover ①).

绿: 落地字段齐全 / 追加写 / 每日文件名 / pending 跨文件汇总排序 /
cleared 排除 / executor 链尾全败接线 / 水位渲染与截断.
红: 不可写返回 None / 目录缺失返回 [] / 坏行跳过 / 成功链路不落地 /
限频抑制不重复落地 / loudness off 不落地.
全部 tmp_path 文件系统 + fake 通道, 零网络零真实飞书.
"""
from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import MagicMock

from delivery_layer.channels.base import DeliveryResult
from delivery_layer.channels.registry import ChannelRegistry
from delivery_layer.executor import DeliveryExecutor
from delivery_layer.message import Message
from delivery_layer.outbox import OutboxStore


def _msg(**kw) -> Message:
    base = {"content": {"k": "v"}, "content_type": "dev_verify", "title": "测试卡"}
    base.update(kw)
    return Message(**base)


class _FakeChannel:
    """返回失败结果的假通道: 不抛异常 → retry 不重试不 sleep, 测试快."""

    def __init__(self, name: str, ok: bool = True, error: str = "boom"):
        self._name, self._ok, self._error = name, ok, error

    @property
    def name(self) -> str:
        return self._name

    def send(self, message: Message) -> DeliveryResult:
        return DeliveryResult(
            message_id=message.message_id, channel=self._name,
            status="success" if self._ok else "failed",
            error_msg="" if self._ok else self._error,
        )

    def health(self) -> bool:
        return True


def _executor(tmp_path, route: list[str], channels: list) -> DeliveryExecutor:
    registry = ChannelRegistry()
    for ch in channels:
        registry.register(ch)
    router = MagicMock()
    router.resolve = MagicMock(return_value=route)
    ex = DeliveryExecutor(registry, router, db=None)
    ex.outbox = OutboxStore(tmp_path)
    return ex


def _daily_path(tmp_path) -> object:
    return tmp_path / f"outbox-{datetime.now():%Y%m%d}.jsonl"


class TestOutboxStoreGreen:
    def test_land_appends_entry_with_all_fields(self, tmp_path):
        store = OutboxStore(tmp_path)
        msg = _msg(content_type="morning_brief", title="早报",
                   severity="info", source="report")
        results = [DeliveryResult(message_id=msg.message_id, channel="feishu_dm",
                                  status="failed", error_msg="timeout",
                                  retry_count=3)]
        path = store.land(msg, ["feishu_dm"], results)
        assert path is not None and path.exists()
        entry = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        assert entry["message_id"] == msg.message_id
        assert entry["content_type"] == "morning_brief"
        assert entry["title"] == "早报"
        assert entry["severity"] == "info"
        assert entry["route"] == ["feishu_dm"]
        assert entry["attempts"] == [{"channel": "feishu_dm", "status": "failed",
                                      "error": "timeout", "retry_count": 3}]
        assert entry["payload"] == {"k": "v"}
        assert entry["ts"]

    def test_land_twice_is_append_only(self, tmp_path):
        store = OutboxStore(tmp_path)
        store.land(_msg(), ["c"], [])
        store.land(_msg(), ["c"], [])
        lines = _daily_path(tmp_path).read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2

    def test_daily_file_named_by_date(self, tmp_path):
        path = OutboxStore(tmp_path).land(_msg(), [], [])
        assert path.name == f"outbox-{datetime.now():%Y%m%d}.jsonl"

    def test_pending_aggregates_and_sorts_across_files(self, tmp_path):
        (tmp_path / "outbox-20260802.jsonl").write_text(
            json.dumps({"ts": "2026-08-02T08:00:00", "message_id": "m2"}) + "\n",
            encoding="utf-8")
        (tmp_path / "outbox-20260801.jsonl").write_text(
            json.dumps({"ts": "2026-08-01T08:00:00", "message_id": "m1"}) + "\n",
            encoding="utf-8")
        ids = [e["message_id"] for e in OutboxStore(tmp_path).pending()]
        assert ids == ["m1", "m2"]

    def test_pending_excludes_cleared(self, tmp_path):
        store = OutboxStore(tmp_path)
        m1, m2 = _msg(), _msg()
        store.land(m1, ["c"], [])
        store.land(m2, ["c"], [])
        assert store.mark_cleared([m1.message_id]) == 1
        left = store.pending()
        assert [e["message_id"] for e in left] == [m2.message_id]

    def test_mark_cleared_returns_count_and_writes_file(self, tmp_path):
        store = OutboxStore(tmp_path)
        assert store.mark_cleared(["a", "b"]) == 2
        assert (tmp_path / "cleared.jsonl").exists()


class TestOutboxStoreRed:
    def test_land_unwritable_returns_none(self, tmp_path):
        blocker = tmp_path / "file"
        blocker.write_text("x", encoding="utf-8")
        store = OutboxStore(blocker / "sub")  # 在普通文件上 mkdir 必败
        assert store.land(_msg(), ["c"], []) is None

    def test_pending_missing_dir_returns_empty(self, tmp_path):
        assert OutboxStore(tmp_path / "nope").pending() == []

    def test_pending_skips_corrupt_and_blank_lines(self, tmp_path):
        _daily_path(tmp_path).write_text(
            "not-json\n"
            + json.dumps({"ts": "2026-08-27T08:00:00", "message_id": "ok"}) + "\n\n",
            encoding="utf-8")
        assert [e["message_id"] for e in OutboxStore(tmp_path).pending()] == ["ok"]

    def test_mark_cleared_empty_returns_zero(self, tmp_path):
        assert OutboxStore(tmp_path).mark_cleared([]) == 0

    def test_cleared_file_survives_corrupt_first_line(self, tmp_path):
        store = OutboxStore(tmp_path)
        m = _msg()
        store.land(m, ["c"], [])
        (tmp_path / "cleared.jsonl").write_text("corrupt\n", encoding="utf-8")
        store.mark_cleared([m.message_id])  # 追加不被坏行影响
        assert store.pending() == []


class _BoomStore:
    def pending(self):
        raise OSError("disk gone")


class TestOutboxWatermarkSection:
    def _section(self):
        from report_layer.sections.outbox_watermark import OutboxWatermarkSection
        return OutboxWatermarkSection()

    def test_no_pending_renders_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QS_OUTBOX_DIR", str(tmp_path))
        assert self._section().build(None) is None

    def test_missing_dir_renders_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QS_OUTBOX_DIR", str(tmp_path / "nope"))
        assert self._section().build(None) is None

    def test_renders_count_content_type_title(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QS_OUTBOX_DIR", str(tmp_path))
        results = [DeliveryResult(message_id="x", channel="feishu_dm",
                                  status="failed", error_msg="timeout")]
        OutboxStore(tmp_path).land(
            _msg(content_type="morning_brief", title="🌅 QuantStar 早报"),
            ["feishu_dm"], results)
        result = self._section().build(None)
        assert result is not None
        assert result.title == "投递水位"
        assert "1 条消息" in result.body
        assert "morning_brief" in result.body
        assert "QuantStar 早报" in result.body
        assert "feishu_dm" in result.body
        assert result.sources == ["outbox"]

    def test_overflow_truncated_to_five(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QS_OUTBOX_DIR", str(tmp_path))
        lines = [json.dumps({"ts": f"2026-08-27T08:0{i}:00", "message_id": f"m{i}",
                             "content_type": "evening_report", "title": f"卡{i}",
                             "attempts": []})
                 for i in range(7)]
        _daily_path(tmp_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
        result = self._section().build(None)
        assert "7 条消息" in result.body
        assert "其余 2 条略" in result.body

    def test_pending_exception_returns_none(self, monkeypatch):
        import report_layer.sections.outbox_watermark as ow
        monkeypatch.setattr(ow, "OutboxStore", _BoomStore)
        assert ow.OutboxWatermarkSection().build(None) is None


class TestExecutorWiring:
    def test_all_channels_fail_lands_outbox(self, tmp_path):
        ex = _executor(tmp_path, route=["feishu_dm"],
                       channels=[_FakeChannel("feishu_dm", ok=False, error="api 500")])
        msg = _msg(content_type="evening_report")
        results = ex.send(msg)
        assert results and all(r.status == "failed" for r in results)
        entries = ex.outbox.pending()
        assert len(entries) == 1
        assert entries[0]["message_id"] == msg.message_id
        assert entries[0]["route"] == ["feishu_dm"]
        assert entries[0]["attempts"][0]["error"] == "api 500"

    def test_raising_channel_lands_with_error(self, tmp_path):
        class _Raiser(_FakeChannel):
            def send(self, message):
                raise ValueError("channel exploded")
        ex = _executor(tmp_path, route=["r"], channels=[_Raiser("r", ok=False)])
        results = ex.send(_msg(content_type="evening_report"))
        assert all(r.status == "failed" for r in results)
        entries = ex.outbox.pending()
        assert len(entries) == 1
        assert "channel exploded" in entries[0]["attempts"][0]["error"]

    def test_success_does_not_land(self, tmp_path):
        ex = _executor(tmp_path, route=["feishu_dm"],
                       channels=[_FakeChannel("feishu_dm", ok=True)])
        results = ex.send(_msg(content_type="evening_report"))
        assert results[0].status == "success"
        assert ex.outbox.pending() == []

    def test_fan_out_partial_success_does_not_land(self, tmp_path):
        ex = _executor(tmp_path, route=["a", "b"],
                       channels=[_FakeChannel("a", ok=False),
                                 _FakeChannel("b", ok=True)])
        results = ex.send(_msg(content_type="intraday_alert"))
        assert len(results) == 2
        assert any(r.status == "success" for r in results)
        assert ex.outbox.pending() == []

    def test_no_healthy_channel_lands_with_empty_attempts(self, tmp_path):
        ex = _executor(tmp_path, route=["ghost"], channels=[])
        results = ex.send(_msg(content_type="evening_report"))
        assert results == []
        entries = ex.outbox.pending()
        assert len(entries) == 1
        assert entries[0]["attempts"] == []
        assert entries[0]["route"] == ["ghost"]

    def test_rate_limited_second_send_does_not_reland(self, tmp_path):
        ex = _executor(tmp_path, route=["feishu_dm"],
                       channels=[_FakeChannel("feishu_dm", ok=False)])
        msg = _msg(content_type="evening_report")
        ex.send(msg)
        ex.send(msg)  # 限频抑制 ≠ 失败, 不得重复落地
        assert len(ex.outbox.pending()) == 1

    def test_loudness_off_does_not_land(self, tmp_path):
        ex = _executor(tmp_path, route=["feishu_dm"],
                       channels=[_FakeChannel("feishu_dm", ok=False)])
        assert ex.send(_msg(loudness="off")) == []
        assert ex.outbox.pending() == []
