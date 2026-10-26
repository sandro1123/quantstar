"""R3/D1 拦截蓝卡接通 — 红绿测试（设计稿-C：check_entry 拦截原因可见化）。

链路：entry_engine 各闸门 stash _block_reason → entry_checker.check 检出
「价格在区间内但被拦」→ monitor._scan 推蓝卡 push_entry(block_reason=...)
且不执行入场。纯增量：check_entry 签名/返回值不变。
"""
import pytest
from unittest.mock import MagicMock, patch

from execution_layer.core.entry_engine import EntryEngine
from execution_layer.intraday.entry_checker import IntradayEntryChecker


def _pool_item(**over):
    item = {
        "code": "688349", "status": "pending_entry", "name": "三一重能",
        "entry_low": 28.0, "entry_high": 30.0, "valid_until": None,
        "stop_loss": 26.0, "added_reason": "风电景气",
    }
    item.update(over)
    return item


def _engine(pool_item):
    sp = MagicMock()
    sp.get.return_value = pool_item
    sp.action_record = MagicMock()
    return EntryEngine(db=MagicMock(), stock_pool=sp)


def _kline(close, open_=None):
    k = MagicMock()
    k.close = close
    k.open = open_ if open_ is not None else close
    return k


@pytest.fixture
def gate_pass():
    """各测试默认放行 ProtectionGate / ConstraintChecker，只测目标闸门。"""
    with patch("execution_layer.core.protection_gate.ProtectionGate") as pg, \
         patch("execution_layer.core.constraint_checker.ConstraintChecker") as cc:
        pg.return_value.check.return_value = (True, "")
        cc.return_value.check_entry.return_value = MagicMock(
            passed=True, violations=[])
        yield pg, cc


class TestEntryEngineBlockReason:
    """各闸门拦截时必须 stash _block_reason，放行/区间外必须为 None。"""

    def test_expired_valid_until_stashes_reason(self, gate_pass):
        eng = _engine(_pool_item(valid_until="2026-08-01"))
        assert eng.check_entry("688349", 29.0, "10:00") is None
        assert eng._block_reason is not None
        assert "过期" in eng._block_reason

    def test_protection_gate_stashes_reason(self, gate_pass):
        pg, _ = gate_pass
        pg.return_value.check.return_value = (False, "回撤熔断中")
        eng = _engine(_pool_item())
        assert eng.check_entry("688349", 29.0, "10:00") is None
        assert eng._block_reason == "回撤熔断中"

    def test_spike_defense_stashes_reason(self, gate_pass):
        dl = MagicMock()
        dl.get_history.return_value = MagicMock(
            klines=[_kline(10.0)] * 14 + [_kline(10.5, open_=10.0)])
        eng = _engine(_pool_item(entry_low=11.0, entry_high=13.0))
        eng.data_layer = dl
        assert eng.check_entry("688349", 12.5, "10:00") is None
        assert eng._block_reason is not None
        assert "追高" in eng._block_reason

    def test_constraint_failure_stashes_reason(self, gate_pass):
        _, cc = gate_pass
        cc.return_value.check_entry.return_value = MagicMock(
            passed=False, violations=["单行业超配"])
        eng = _engine(_pool_item())
        assert eng.check_entry("688349", 29.0, "10:00") is None
        assert eng._block_reason is not None
        assert "单行业超配" in eng._block_reason

    def test_price_out_of_range_no_block_reason(self, gate_pass):
        eng = _engine(_pool_item())
        assert eng.check_entry("688349", 25.0, "10:00") is None
        assert eng._block_reason is None

    def test_success_path_no_block_reason(self, gate_pass):
        eng = _engine(_pool_item())
        decision = eng.check_entry("688349", 29.0, "10:00")
        assert decision is not None and decision.action == "buy"
        assert eng._block_reason is None

    def test_block_reason_reset_between_calls(self, gate_pass):
        eng = _engine(_pool_item(valid_until="2026-08-01"))
        eng.check_entry("688349", 29.0, "10:00")
        assert eng._block_reason is not None
        # 第二次调用（新池项无过期）必须重置, 不得残留上一笔原因
        eng.stock_pool.get.return_value = _pool_item()
        eng.check_entry("688349", 29.0, "10:00")
        assert eng._block_reason is None


class TestEntryCheckerBlocked:
    """check(): 价格在区间内但被闸门拦 → blocked 结构；区间外/无拦截 → None。"""

    def _checker(self, engine):
        return IntradayEntryChecker(MagicMock(), MagicMock(), engine,
                                    data_layer=None)

    def test_blocked_in_range_returns_blocked_struct(self):
        eng = MagicMock()
        eng.check_entry.return_value = None
        eng._block_reason = "回撤熔断中"
        chk = self._checker(eng)
        chk._get_current_price = MagicMock(return_value=29.0)
        result = chk.check("688349", _pool_item())
        assert result is not None
        assert result["blocked"] is True
        assert result["triggered"] is False
        assert result["price"] == 29.0
        assert "回撤熔断" in result["reason"]

    def test_no_block_reason_returns_none(self):
        eng = MagicMock()
        eng.check_entry.return_value = None
        eng._block_reason = None
        chk = self._checker(eng)
        chk._get_current_price = MagicMock(return_value=29.0)
        assert chk.check("688349", _pool_item()) is None

    def test_blocked_but_price_out_of_range_returns_none(self):
        eng = MagicMock()
        eng.check_entry.return_value = None
        eng._block_reason = "回撤熔断中"
        chk = self._checker(eng)
        chk._get_current_price = MagicMock(return_value=25.0)
        assert chk.check("688349", _pool_item()) is None

    def test_triggered_path_unchanged(self):
        eng = MagicMock()
        eng.check_entry.return_value = MagicMock(price=29.0, reason="触价")
        chk = self._checker(eng)
        chk._get_current_price = MagicMock(return_value=29.0)
        result = chk.check("688349", _pool_item())
        assert result["triggered"] is True
        assert "blocked" not in result


class TestMonitorBlueCard:
    """_scan: blocked 结果 → 推蓝卡(block_reason) + 不执行入场 + 留痕 blocked。"""

    def _monitor(self, entry_result):
        from execution_layer.intraday.monitor import IntradayMonitor
        sp = MagicMock()
        sp.get_by_status.side_effect = lambda s: (
            [_pool_item()] if s == "pending_entry" else [])
        m = IntradayMonitor(db=MagicMock(), stock_pool=sp)
        m.entry_checker = MagicMock()
        m.entry_checker.check.return_value = entry_result
        m.exit_checker = None
        m.paper_broker = MagicMock()
        m.touch_card = MagicMock()
        return m

    def test_blocked_pushes_blue_card_and_skips_entry(self):
        m = self._monitor({"price": 29.0, "reason": "回撤熔断中",
                           "triggered": False, "blocked": True})
        m._scan()
        # 蓝卡照推, 带拦截原因
        m.touch_card.push_entry.assert_called_once()
        kwargs = m.touch_card.push_entry.call_args.kwargs
        assert "回撤熔断" in kwargs.get("block_reason", "")
        # 绝不执行入场
        m.paper_broker.entry_with_fees.assert_not_called()

    def test_blocked_records_blocked_status_row(self):
        m = self._monitor({"price": 29.0, "reason": "回撤熔断中",
                           "triggered": False, "blocked": True})
        m._scan()
        rows = m.db.get_connection().executemany.call_args.args[1]
        statuses = [r[6] for r in rows]
        assert "blocked" in statuses

    def test_triggered_path_still_executes_entry(self):
        m = self._monitor({"price": 29.0, "reason": "触价", "triggered": True})
        m._scan()
        m.paper_broker.entry_with_fees.assert_called_once()
        m.touch_card.push_entry.assert_called_once()
        assert m.touch_card.push_entry.call_args.kwargs.get("block_reason") is None
