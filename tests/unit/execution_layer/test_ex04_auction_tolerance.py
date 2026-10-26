"""EX-04: 竞价入场触价容差 — 09:25-09:30 浮点严格相等死分支

entry_engine.py:117 竞价时段用 current_price == entry_low 浮点严格相等,
实盘竞价价格几乎不可能与 entry_low 完全相等 → 死分支, 竞价入场永不触发。
新契约: abs(current_price - entry_low) <= entry_low * 0.002 (±0.2% 容差)。

先写失败测试: ±0.1% 应触发 (当前 == 不触发)。
"""
from unittest.mock import MagicMock

import pytest

from common.database import Database
from execution_layer.core.entry_engine import EntryEngine


def _make_engine(entry_low=10.0, entry_high=11.0):
    db = Database(":memory:")
    pool = MagicMock()
    pool.get.return_value = {
        "code": "000001", "status": "pending_entry",
        "entry_low": entry_low, "entry_high": entry_high, "exit_date": None,
    }
    return EntryEngine(db, pool, agent_layer=None)


def test_auction_exact_price_triggers():
    """回归: 竞价价恰等于 entry_low 仍触发"""
    engine = _make_engine(entry_low=10.0)
    result = engine.check_entry("000001", 10.0, "09:26:00")
    assert result is not None
    assert result.action == "buy"


def test_auction_plus_0_1pct_triggers():
    """失败测试: 竞价价 +0.1% (容差内) 应触发"""
    engine = _make_engine(entry_low=10.0)
    result = engine.check_entry("000001", 10.01, "09:26:00")
    assert result is not None, "+0.1% 在 ±0.2% 容差内应触发竞价入场"
    assert result.action == "buy"


def test_auction_minus_0_1pct_triggers():
    """失败测试: 竞价价 -0.1% (容差内) 应触发"""
    engine = _make_engine(entry_low=10.0)
    result = engine.check_entry("000001", 9.99, "09:26:00")
    assert result is not None, "-0.1% 在 ±0.2% 容差内应触发竞价入场"
    assert result.action == "buy"


def test_auction_plus_0_3pct_no_trigger():
    """竞价价 +0.3% (容差外) 不触发"""
    engine = _make_engine(entry_low=10.0)
    result = engine.check_entry("000001", 10.03, "09:26:00")
    assert result is None, "+0.3% 超出 ±0.2% 容差不应触发"


def test_auction_minus_0_3pct_no_trigger():
    """竞价价 -0.3% (容差外) 不触发"""
    engine = _make_engine(entry_low=10.0)
    result = engine.check_entry("000001", 9.97, "09:26:00")
    assert result is None, "-0.3% 超出 ±0.2% 容差不应触发"


def test_auction_tolerance_scales_with_price():
    """容差随价格缩放: entry_low=100 时 ±0.2 (0.2%) 内触发"""
    engine = _make_engine(entry_low=100.0, entry_high=110.0)
    assert engine.check_entry("000001", 100.15, "09:26:00") is not None
    assert engine.check_entry("000001", 100.5, "09:26:00") is None
