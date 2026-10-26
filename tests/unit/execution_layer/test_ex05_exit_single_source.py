"""EX-05: 退出规则单一源 — exit_checker 硬编码短路并入 exit_engine

exit_checker.py 原在调 6 层引擎前硬编码两条短路:
  price <= stop_loss            → 直接触发 (绕过引擎, 也绕过 T+1 保护)
  price >= entry_price * 1.2    → 20% 硬编码止盈 (无视持仓自身 take_profit 参数)
新契约: checker 只保留 取价 + 引擎调用 + 触发标记; 止损/止盈统一走 exit_engine
layer 1 (消费持仓自身 stop_loss/take_profit 参数)。

先写失败测试: take_profit=+10% 的持仓, 触发应来自引擎(带 exit_low/action 的完整
触发标记), 而非硬编码 20% 短路的降级 dict。
"""
from unittest.mock import MagicMock

import pytest

from common.database import Database
from execution_layer.core.exit_engine import ExitEngine
from execution_layer.intraday.exit_checker import IntradayExitChecker


def _make_checker(price, row, engine_pool=None):
    """真实 ExitEngine + MagicMock pool (row 由测试控制) + data_layer 控价"""
    db = Database(":memory:")
    pool = MagicMock()
    pool.get.return_value = row
    engine = ExitEngine(db, engine_pool or pool, agent_layer=None)
    engine.action_record._ensure_table()
    data_layer = MagicMock()
    data_layer.get_realtime_prices.return_value = {"000001": price}
    data_layer.get_history.return_value = None  # 无 K 线, layer4 干净跳过
    checker = IntradayExitChecker(db, pool, engine, data_layer=data_layer)
    return checker


def _active_row(**over):
    row = {
        "code": "000001", "status": "active", "entry_price": 10.0,
        "entry_date": "2026-07-01",  # 非今日, 不触发 T+1
        "stop_loss": 9.0, "take_profit": 11.0,
        "dynamic_stop_loss": 0, "atr_stop": 0, "trailing_stop": 0,
        "highest_price": 0, "holding_days": 0,
        "current_score": 0, "initial_score": 0,
    }
    row.update(over)
    return row


def test_take_profit_10pct_triggers_at_10pct():
    """take_profit=+10% (11.0) 的持仓, 价格到 11.0 应触发止盈"""
    checker = _make_checker(11.0, _active_row(take_profit=11.0))
    result = checker.check("000001", {"code": "000001", "entry_price": 10.0,
                                      "stop_loss": 9.0, "take_profit": 11.0})
    assert result is not None, "take_profit=+10% 应在 +10% 触发"
    assert result["triggered"] is True


def test_take_profit_trigger_comes_from_engine_not_hardcoded():
    """失败测试: take_profit=+10%, 价格涨到 +20% — 触发必须来自引擎 (带 exit_low/action),
    而非硬编码 20% 短路的降级 dict (无 exit_low/action)"""
    checker = _make_checker(12.0, _active_row(take_profit=11.0))
    result = checker.check("000001", {"code": "000001", "entry_price": 10.0,
                                      "stop_loss": 9.0, "take_profit": 11.0})
    assert result is not None
    assert result.get("exit_low"), "触发应来自引擎, 带完整 exit_low (硬编码短路无此字段)"
    assert result.get("action") == "sell", "触发应来自引擎, 带 action 标记"


def test_hardcoded_20pct_no_longer_overrides_own_take_profit():
    """失败测试: take_profit=+30% (13.0), 价格 +20% (12.0) — 不应触发
    (原硬编码 20% 会无视持仓自身止盈参数提前触发)"""
    checker = _make_checker(12.0, _active_row(take_profit=13.0))
    result = checker.check("000001", {"code": "000001", "entry_price": 10.0,
                                      "stop_loss": 9.0, "take_profit": 13.0})
    assert result is None, "持仓自身 take_profit=+30%, +20% 不应触发 (硬编码 20% 已移除)"


def test_stop_loss_trigger_comes_from_engine():
    """失败测试: 跌破止损 — 触发应来自引擎 layer1 (带 exit_low), 非硬编码短路"""
    checker = _make_checker(8.5, _active_row(stop_loss=9.0))
    result = checker.check("000001", {"code": "000001", "entry_price": 10.0,
                                      "stop_loss": 9.0, "take_profit": 11.0})
    assert result is not None
    assert result.get("exit_low"), "止损触发应来自引擎, 带完整 exit_low"
    assert result.get("action") == "sell"


def test_t1_position_no_same_day_exit():
    """失败测试: 今日买入 (T+0) 跌破止损 — 不应产生卖出触发 (A 股 T+1,
    原硬编码短路绕过引擎 T+1 保护会非法触发)"""
    from common.utils import bj_date
    checker = _make_checker(8.5, _active_row(stop_loss=9.0, entry_date=bj_date()))
    result = checker.check("000001", {"code": "000001", "entry_price": 10.0,
                                      "stop_loss": 9.0, "take_profit": 11.0,
                                      "entry_date": bj_date()})
    assert result is None, "T+0 持仓不能当日卖出, 硬编码短路绕过 T+1 属非法触发"


def test_price_unavailable_no_trigger():
    """回归: 取价失败 → 不触发"""
    db = Database(":memory:")
    pool = MagicMock()
    engine = ExitEngine(db, pool, agent_layer=None)
    data_layer = MagicMock()
    data_layer.get_realtime_prices.return_value = {}  # 实时价不可用
    checker = IntradayExitChecker(db, pool, engine, data_layer=data_layer)
    result = checker.check("000001", {"code": "000001", "entry_price": 10.0,
                                      "stop_loss": 9.0})
    assert result is None


def test_no_engine_no_trigger():
    """回归: 无引擎 → 不触发 (checker 不再自带硬编码退出规则)"""
    db = Database(":memory:")
    pool = MagicMock()
    data_layer = MagicMock()
    data_layer.get_realtime_prices.return_value = {"000001": 8.5}
    checker = IntradayExitChecker(db, pool, None, data_layer=data_layer)
    result = checker.check("000001", {"code": "000001", "entry_price": 10.0,
                                      "stop_loss": 9.0})
    assert result is None, "退出规则单一源=引擎; 无引擎时 checker 不应自行判定退出"
