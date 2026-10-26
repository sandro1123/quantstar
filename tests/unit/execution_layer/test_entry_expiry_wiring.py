"""S5: 入场区间过期检查 — valid_until 消费 + 初始入场链写入"""
from datetime import date, timedelta

import pytest

from common.database import Database
from execution_layer import ExecutionLayer


@pytest.fixture()
def el():
    db = Database(":memory:")
    db.init_db()
    el = ExecutionLayer(db=db)
    el.start()
    yield el
    el.shutdown()
    db.close()


def test_expired_valid_until_rejects_entry(el):
    """valid_until 已过期 → 拒单 + skip_entry 留痕 (防按陈旧计划买入)"""
    el.stock_pool.add("000001", "陈旧单")
    el.stock_pool.update_status("000001", "pending_entry")
    el.stock_pool.update("000001", entry_low=10.0, entry_high=11.0,
                         valid_until="2020-01-01")
    decision = el.entry_engine.check_entry("000001", 10.5, "10:00")
    assert decision is None, "过期入场区间必须拒单"
    conn = el.db.get_connection()
    skips = conn.execute(
        "SELECT reason FROM action_records "
        "WHERE code='000001' AND action_type='skip_entry'"
    ).fetchall()
    assert any("过期" in r["reason"] for r in skips), "拒单应留 skip_entry 痕"


def test_not_expired_valid_until_allows_entry(el):
    """valid_until 未过期 → 不因过期拒单 (竞合窗口触价正常入场)"""
    tomorrow = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")
    el.stock_pool.add("000002", "有效单")
    el.stock_pool.update_status("000002", "pending_entry")
    el.stock_pool.update("000002", entry_low=10.0, entry_high=11.0,
                         valid_until=tomorrow)
    decision = el.entry_engine.check_entry("000002", 10.0, "09:26")
    assert decision is not None, "未过期且触价应产生入场决策"
    assert decision.action == "buy"


def test_null_valid_until_not_blocked(el):
    """valid_until 为空 → 诚实维持现状不拦 (存量语义, 写入侧已补)"""
    el.stock_pool.add("000003", "空值单")
    el.stock_pool.update_status("000003", "pending_entry")
    el.stock_pool.update("000003", entry_low=10.0, entry_high=11.0)
    decision = el.entry_engine.check_entry("000003", 10.0, "09:26")
    assert decision is not None, "valid_until 为空不应触发过期拒单"


def test_entry_valid_until_helper_future_date(el):
    """entry_valid_until = 今日 + N 交易日, 必须落在未来"""
    from execution_layer.core.plan_manager import entry_valid_until
    result = entry_valid_until(el.db)
    assert result > date.today().strftime("%Y-%m-%d"), "有效期必须在未来"


def test_plan_manager_writes_valid_until_on_entry(el):
    """接线守卫: plan_manager 两处 buy 分支源码必须写 valid_until"""
    import inspect
    from execution_layer.core import plan_manager
    src_code = inspect.getsource(plan_manager)
    assert src_code.count("valid_until=entry_valid_until") >= 2, \
        "receive_plan 与 consume_approved 两处 buy 分支都应写 valid_until"
