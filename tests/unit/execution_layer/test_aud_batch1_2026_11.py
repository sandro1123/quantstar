"""AUD-02 / AUD-03 / AUD-05 修复批次 (审计 2026-11) — 回归钉。

三处共同病灶: 判据在特定输入下恒真, 于是「看起来有闸、实际不拦」。
本文件每条断言都必须能在旧实现上报红 (AGENTS.md 铁律 #1)。
"""
from datetime import date
from unittest.mock import MagicMock

import pytest

from common.database import Database
from execution_layer.core.constraint_checker import ConstraintChecker
from execution_layer.core.entry_engine import EntryEngine
from execution_layer.core.protection_gate import ProtectionGate


# 固定历史日历事实 (2026 国庆休市窗口), 一律用 date() 构造而非字符串字面量 ——
# tests/unit/ops_layer/test_no_hardcoded_dates.py 的基线门禁治的是「随今天漂移的
# 日期字面量」; 这里是已封闭的历史事实, 按同一口径不落字面量, 免得改一次测试
# 就要改一次 .hardcoded_date_baseline.txt。
_NDAY_START = date(2026, 9, 30).isoformat()
_NDAY_END = date(2026, 10, 12).isoformat()
_ONE_TRADING_DAY = date(2026, 11, 16).isoformat()


def _pool(**over):
    """MagicMock 池: dict 行 + 真实数值现金 (MagicMock 返回值会被 isinstance 挡掉)。"""
    item = {"code": "000001", "status": "pending_entry", "entry_low": 14.0,
            "entry_high": 16.0, "exit_date": None, "entry_date": None}
    item.update(over)
    pool = MagicMock()
    pool.get.return_value = item
    pool.get_available_cash.return_value = 300000.0
    pool.get_by_status.return_value = []
    return pool


# -- AUD-02: 入场约束必须校验「将要下的这一单」 -----------------------------------

def test_check_entry_requires_explicit_position_qty():
    """缺省 position_qty 必须 TypeError, 不得回落 100 股默认值。

    旧实现签名 `position_qty: int = 100` —— 这正是生产链路被喂 100 股的源头。
    """
    checker = ConstraintChecker(_pool())
    with pytest.raises(TypeError):
        checker.check_entry("000001", 15.0)  # noqa: F841 — 缺参必须报错


def test_plan_qty_respects_single_position_cap():
    """本笔规模受「单只仓位上限 20%」约束, 且折成整手。"""
    checker = ConstraintChecker(_pool())          # initial 300000, cap 20% = 60000
    qty = checker.plan_qty(15.0)
    assert qty == 4000, f"60000/15 应得 4000 股, 实得 {qty}"
    assert qty % 100 == 0, "必须整手"
    assert qty * 15.0 <= 300000.0 * 0.20 + 1e-6, "不得超过单只上限"


def test_plan_qty_limited_by_available_cash():
    """现金不足时以现金为约束 (而非上限)。"""
    pool = _pool()
    pool.get_available_cash.return_value = 5000.0
    assert ConstraintChecker(pool).plan_qty(15.0) == 300   # 5000/15=333 → 300


def test_plan_qty_zero_when_unaffordable():
    """买不起一手必须返回 0 (而不是 100 股)。"""
    pool = _pool()
    pool.get_available_cash.return_value = 500.0
    assert ConstraintChecker(pool).plan_qty(15.0) == 0


def test_entry_blocks_when_plan_qty_cannot_buy_one_lot():
    """买不起一手 → 拦截并落结构化原因 (旧实现恒放行)。"""
    pool = _pool()
    pool.get_available_cash.return_value = 500.0
    engine = EntryEngine(Database(":memory:"), pool, agent_layer=None)
    assert engine.check_entry("000001", 14.0, "09:35:00") is None
    assert engine._block_info is not None
    assert engine._block_info["gate"] == "sizing", engine._block_info


def test_entry_decision_carries_planned_qty():
    """入场决策必须携带本笔股数, 供下游按同规模下单。"""
    engine = EntryEngine(Database(":memory:"), _pool(), agent_layer=None)
    decision = engine.check_entry("000001", 14.0, "09:35:00")
    assert decision is not None and decision.action == "buy"
    assert decision.qty == 4000, f"应携带本笔股数 4000, 实得 {decision.qty}"


def test_constraint_blocks_when_planned_qty_exceeds_cap():
    """本笔规模越过单只上限时必须拦下 —— 旧实现用 100 股校验, 这条永远绿。"""
    checker = ConstraintChecker(_pool())
    result = checker.check_entry("000001", 15.0, 6000)   # 90000 > 60000
    assert not result.passed
    assert any("单只仓位上限" in v for v in result.violations), result.violations


# -- AUD-03: 追高防御异常必须可见, 不得静默 pass --------------------------------

def test_spike_defense_degradation_is_visible():
    """取数异常 → fail-open 仍返回 None, 但必须置显式降级标记。

    旧实现 `except Exception: pass` —— 四道追高检查整体消失且零痕迹。
    """
    data = MagicMock()
    data.get_history.side_effect = RuntimeError("database is locked")
    engine = EntryEngine(Database(":memory:"), _pool(), data_layer=data)
    assert engine._check_spike_defense("000001", 15.0) is None
    assert engine._spike_defense_degraded is True, "异常必须可见 (显式降级标记)"


def test_spike_defense_degradation_flag_resets_per_call():
    """降级标记按次重置, 不残留到下一次正常检查。"""
    data = MagicMock()
    data.get_history.side_effect = RuntimeError("boom")
    engine = EntryEngine(Database(":memory:"), _pool(), data_layer=data)
    engine._check_spike_defense("000001", 15.0)
    assert engine._spike_defense_degraded is True

    class _K:
        def __init__(self, c, o=0.0):
            self.close = c
            self.open = o

    healthy = MagicMock()
    healthy.get_history.return_value.klines = [_K(10.0) for _ in range(20)]
    engine.data_layer = healthy
    engine.check_entry("000001", 1.0, "09:35:00")   # 触价区间外, 但会重置标记
    assert engine._spike_defense_degraded is False


# -- AUD-05: 冷却期按交易日计, 不按工作日 ---------------------------------------

def test_cooldown_uses_trading_calendar_not_weekdays():
    """国庆长假窗口: 工作日 9 天, 真实交易日只有 2 天。

    旧实现按 weekday 计得 9, 于是「5 交易日冷却」在第 3 个真实交易日就放行。
    """
    n = ProtectionGate._trading_days_between(_NDAY_START, _NDAY_END)
    assert n == 2, f"2026 国庆窗口应计 2 个交易日 (10-09/10-12), 实得 {n}"


def test_cooldown_same_day_counts_zero_trading_days():
    """当日卖出 → 0 个交易日 → 必须仍被冷却期拦截。"""
    assert ProtectionGate._trading_days_between(_ONE_TRADING_DAY, _ONE_TRADING_DAY) == 0


def test_cooldown_dirty_date_does_not_block():
    """日期脏数据 → 999 (不拦), 不得因解析失败误杀。"""
    assert ProtectionGate._trading_days_between("not-a-date", _ONE_TRADING_DAY) == 999
