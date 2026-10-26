"""R2: 盘中触价行动卡测试 — touch_card.py 卡片契约 + _scan 并联接入。

覆盖统筹派单五条断言: 触发/去重/色彩/措辞/合规尾。
hermetic: QS_TOUCH_CARD_DIR 指 tmp_path (jsonl 台账隔离), delivery 全 mock,
零网络零生产库。接入侧 touch_card 注入 MagicMock, 断言并联调用契约。
"""
import json
import os
from unittest.mock import MagicMock

import pytest

from execution_layer.intraday import touch_card as tc
from execution_layer.intraday.touch_card import (
    COMPLIANCE_FOOTER,
    HEADER_BLUE,
    HEADER_GREEN,
    HEADER_ORANGE,
    HEADER_RED,
    TouchCardPusher,
)


class _FakeDelivery:
    """捕获 send 调用的假投递层 (断言卡片内容/头色)。"""

    def __init__(self):
        self.sent = []

    def send(self, msg, report_mode="debug"):
        self.sent.append(msg)


@pytest.fixture
def pusher(tmp_path, monkeypatch):
    monkeypatch.setenv("QS_TOUCH_CARD_DIR", str(tmp_path))
    p = TouchCardPusher(db=MagicMock(), stock_pool=MagicMock())
    p._delivery = _FakeDelivery()
    p._delivery_ready = True
    return p


POOL_ITEM = {
    "name": "三一重能", "entry_low": 13.79, "entry_high": 14.15,
    "stop_loss": 13.26, "dynamic_stop_loss": None, "take_profit": 15.0,
    "valid_until": "2099-01-01", "added_reason": "候选池第 8/15 位",
}


def _card(pusher) -> dict:
    return pusher._delivery.sent[-1].content["card"]


class TestEntryCard:
    def test_entry_card_green_wording_compliance(self, pusher):
        """触发+色彩+措辞+合规尾: 绿卡, 主句可以买, 参考卖出线, 合规尾。"""
        trade = MagicMock(success=True, qty=100, reason="ok")
        assert pusher.push_entry("688349", 14.0, POOL_ITEM, trade_result=trade)
        card = _card(pusher)
        assert card["header"]["template"] == HEADER_GREEN
        body = card["elements"][0]["text"]["content"]
        assert "可以买" in body
        assert "参考卖出线" in body
        assert "止损" not in body          # 措辞表: 止损线→参考卖出线
        assert COMPLIANCE_FOOTER in body    # 合规尾每卡必带
        assert "模拟买入 100 股" in body    # 系统动作降佐证行

    def test_entry_blocked_card_blue_with_reason(self, pusher):
        """D1 拦截照推: 蓝卡, 主句不变, 标注系统未买入原因。"""
        assert pusher.push_entry("688349", 14.0, POOL_ITEM,
                                 block_reason="风控闸门拦截")
        card = _card(pusher)
        assert card["header"]["template"] == HEADER_BLUE
        body = card["elements"][0]["text"]["content"]
        assert "可以买" in body
        assert "系统本笔未买入：风控闸门拦截" in body
        assert COMPLIANCE_FOOTER in body

    def test_entry_dedup_one_shot_same_day(self, pusher):
        """去重: 一次性卡型当日第二张跳过 (跨进程 jsonl 台账)。"""
        assert pusher.push_entry("688349", 14.0, POOL_ITEM)
        assert not pusher.push_entry("688349", 14.0, POOL_ITEM)
        assert len(pusher._delivery.sent) == 1

    def test_ledger_written(self, pusher):
        """留痕第三落点: jsonl 台账记录 code/card_type。"""
        pusher.push_entry("688349", 14.0, POOL_ITEM)
        files = list(tc._events_dir().glob("touch_card-*.jsonl"))
        assert files
        rec = json.loads(files[0].read_text(encoding="utf-8").splitlines()[0])
        assert rec["code"] == "688349"
        assert rec["card_type"] == "entry"


class TestStopCards:
    def test_approach_card_orange(self, pusher):
        assert pusher.push_stop_approach("600001", 9.7, 9.6, POOL_ITEM)
        card = _card(pusher)
        assert card["header"]["template"] == HEADER_ORANGE
        body = card["elements"][0]["text"]["content"]
        assert "参考卖出线" in body
        assert COMPLIANCE_FOOTER in body

    def test_approach_cooldown_suppresses_second(self, pusher):
        assert pusher.push_stop_approach("600001", 9.7, 9.6, POOL_ITEM)
        assert not pusher.push_stop_approach("600001", 9.7, 9.6, POOL_ITEM)
        assert len(pusher._delivery.sent) == 1

    def test_trigger_card_red(self, pusher):
        assert pusher.push_stop_trigger("600001", 9.4, "跌破参考卖出线", POOL_ITEM)
        card = _card(pusher)
        assert card["header"]["template"] == HEADER_RED
        body = card["elements"][0]["text"]["content"]
        assert "参考卖出线" in body
        assert COMPLIANCE_FOOTER in body


class TestScanWiring:
    """_scan 并联接入: 触发必推卡, 未注入零推送, 主流程行为不变。"""

    def _db(self):
        import sqlite3

        class _FakeDB:
            def __init__(self):
                self.con = sqlite3.connect(":memory:")
                self.con.execute(
                    "CREATE TABLE intraday_checks (check_time TEXT, "
                    "check_round TEXT, code TEXT, check_type TEXT, "
                    "current_price REAL, trigger_price REAL, status TEXT, "
                    "detail TEXT, latency_ms INTEGER)")
                self.con.execute(
                    "CREATE TABLE intraday_pnl_snapshots (snapshot_time TEXT, "
                    "unrealized_pnl_pct REAL, active_count INTEGER)")

            def get_connection(self):
                return self.con

            def execute(self, *a, **kw):
                return self.con.execute(*a, **kw)

        return _FakeDB()

    def _monitor(self, active=None, pending=None):
        from execution_layer.intraday.monitor import IntradayMonitor
        pool = MagicMock()
        pool.get_by_status.side_effect = lambda s: {
            "active": active or [], "pending_entry": pending or [],
        }.get(s, [])
        return IntradayMonitor(self._db(), pool)

    def test_entry_trigger_pushes_green_card(self):
        monitor = self._monitor(pending=[{"code": "000002", "last_price": 12.0}])
        monitor.entry_checker = MagicMock()
        monitor.entry_checker.check.return_value = {
            "price": 11.5, "reason": "触价", "triggered": True}
        monitor.paper_broker = MagicMock()
        monitor.touch_card = MagicMock()
        monitor._scan()
        monitor.touch_card.push_entry.assert_called_once()
        args, kwargs = monitor.touch_card.push_entry.call_args
        assert args[0] == "000002"
        assert args[1] == 11.5
        assert "trade_result" in kwargs

    def test_exit_trigger_pushes_red_card(self):
        item = {"code": "000001", "last_price": 9.5, "stop_loss": 9.0}
        monitor = self._monitor(active=[item])
        monitor.exit_checker = MagicMock()
        monitor.exit_checker.check.return_value = {
            "price": 8.9, "reason": "跌破参考卖出线", "triggered": True,
            "action": "sell", "exit_low": 8.8}
        monitor.touch_card = MagicMock()
        monitor._scan()
        monitor.touch_card.push_stop_trigger.assert_called_once()
        monitor.stock_pool.mark_exit_trigger.assert_called_once()  # 原行为不变

    def test_no_pusher_zero_push(self):
        """负对照: 未注入 touch_card (默认) → 触发也零推送。"""
        monitor = self._monitor(pending=[{"code": "000002", "last_price": 12.0}])
        monitor.entry_checker = MagicMock()
        monitor.entry_checker.check.return_value = {
            "price": 11.5, "reason": "触价", "triggered": True}
        monitor.paper_broker = MagicMock()
        monitor._scan()  # touch_card=None, 不抛即零推送
        assert monitor.touch_card is None

    def test_pusher_exception_does_not_break_scan(self):
        monitor = self._monitor(pending=[{"code": "000002", "last_price": 12.0}])
        monitor.entry_checker = MagicMock()
        monitor.entry_checker.check.return_value = {
            "price": 11.5, "reason": "触价", "triggered": True}
        monitor.paper_broker = MagicMock()
        monitor.touch_card = MagicMock()
        monitor.touch_card.push_entry.side_effect = RuntimeError("boom")
        monitor._scan()  # 推送异常不阻断主流程
        rows = monitor.db.execute(
            "SELECT status FROM intraday_checks").fetchall()
        assert rows and rows[0][0] == "triggered"

    def test_approach_card_on_near_stop(self):
        """逼近橙卡: 未触发但现价距参考卖出线 ≤ 2%。"""
        item = {"code": "000001", "last_price": 9.7, "stop_loss": 9.6}
        monitor = self._monitor(active=[item])
        monitor.exit_checker = MagicMock()
        monitor.exit_checker.check.return_value = None  # 未触发
        monitor.exit_checker._get_current_price.return_value = 9.7
        monitor.touch_card = MagicMock()
        monitor._scan()
        monitor.touch_card.push_stop_approach.assert_called_once()
