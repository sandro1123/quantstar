"""M2: 风控三件套闸门 — 冷却期/连损日停/回撤熔断(迟滞带) + 接线守卫"""
from datetime import date, timedelta
from common.utils import bj_date

import pytest

from common.database import Database
from execution_layer import ExecutionLayer
from execution_layer.core.protection_gate import ProtectionGate


@pytest.fixture()
def el():
    db = Database(":memory:")
    db.init_db()
    el = ExecutionLayer(db=db)
    el.start()
    yield el
    el.shutdown()
    db.close()


def _gate(el):
    return ProtectionGate(el.db, el.stock_pool, config=None)


def _write_exit(el, code, reason, created_at):
    conn = el.db.get_connection()
    conn.execute(
        "INSERT INTO action_records (code, action_type, reason, source, created_at) "
        "VALUES (?, 'exit', ?, 'execution', ?)", (code, reason, created_at))
    conn.commit()


# -- 守卫①: 冷却期 -------------------------------------------------------------

def test_cooldown_blocks_recent_exit(el):
    """卖出当日买回同股 → 拦截"""
    _write_exit(el, "000001", "止盈", f"{bj_date()} 10:00:00")
    can, reason = _gate(el).check("000001", "14:00")
    assert not can
    assert "冷却期" in reason


def test_cooldown_allows_after_5_trading_days(el):
    """卖出已过 5 交易日 → 放行"""
    old_day = (date.today() - timedelta(days=10)).strftime("%Y-%m-%d")
    _write_exit(el, "000001", "止盈", f"{old_day} 10:00:00")
    can, reason = _gate(el).check("000001", "14:00")
    assert can, f"5交易日后应放行: {reason}"


def test_cooldown_is_per_code(el):
    """冷却期只限同股 — 其他股票不受影响"""
    _write_exit(el, "000001", "止损", f"{bj_date()} 10:00:00")
    can, _ = _gate(el).check("000002", "14:00")
    assert can


# -- 守卫②: 当日连损 -----------------------------------------------------------

def test_daily_stoploss_blocks_after_3(el):
    """当日 3 次止损 → 当日停开新仓 (任意股票)"""
    from common.utils import bj_date
    today = bj_date()
    for i, code in enumerate(["000001", "000002", "000003"]):
        _write_exit(el, code, "止损", f"{today} 10:0{i}:00")
    can, reason = _gate(el).check("000009", "14:00")
    assert not can
    assert "连损熔断" in reason


def test_daily_stoploss_allows_below_threshold(el):
    """当日 2 次止损 (< 3) → 放行"""
    from common.utils import bj_date
    today = bj_date()
    _write_exit(el, "000001", "止损", f"{today} 10:00:00")
    _write_exit(el, "000002", "止损", f"{today} 10:01:00")
    can, _ = _gate(el).check("000009", "14:00")
    assert can


def test_stoploss_count_is_daily_scoped(el):
    """昨日 3 次止损不拦今日 (日粒度)"""
    yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    for i, code in enumerate(["000001", "000002", "000003"]):
        _write_exit(el, code, "止损", f"{yesterday} 10:0{i}:00")
    can, _ = _gate(el).check("000009", "14:00")
    assert can


# -- 守卫③: 回撤熔断 (迟滞带) ---------------------------------------------------

def test_drawdown_pause_blocks(el, monkeypatch):
    """回撤 <= -8% → 拦截 + PORTFOLIO volatility_alert 留痕"""
    g = _gate(el)
    monkeypatch.setattr(g, "_current_drawdown", lambda: -0.09)
    can, reason = g.check("000001", "14:00")
    assert not can
    assert "回撤熔断" in reason
    rows = el.db.get_connection().execute(
        "SELECT reason FROM action_records WHERE code='PORTFOLIO' "
        "AND action_type='volatility_alert'").fetchall()
    assert any("回撤熔断:" in r["reason"] for r in rows), "首触应留痕"


def test_drawdown_hysteresis_band_keeps_pause(el, monkeypatch):
    """已熔断且回撤 -6% (在 -8%~-5% 迟滞带) → 维持拦截"""
    g = _gate(el)
    monkeypatch.setattr(g, "_current_drawdown", lambda: -0.09)
    g.check("000001", "14:00")  # 触发熔断留痕
    monkeypatch.setattr(g, "_current_drawdown", lambda: -0.06)
    can, reason = g.check("000002", "14:01")
    assert not can
    assert "迟滞带" in reason


def test_drawdown_resume_allows_and_records(el, monkeypatch):
    """已熔断且回撤回升到 -4% (>= -5%) → 放行 + 解除留痕"""
    g = _gate(el)
    monkeypatch.setattr(g, "_current_drawdown", lambda: -0.09)
    g.check("000001", "14:00")  # 触发熔断
    monkeypatch.setattr(g, "_current_drawdown", lambda: -0.04)
    can, _ = g.check("000002", "14:01")
    assert can
    rows = el.db.get_connection().execute(
        "SELECT reason FROM action_records WHERE code='PORTFOLIO' "
        "AND action_type='volatility_alert'").fetchall()
    assert any("回撤熔断解除:" in r["reason"] for r in rows), "恢复应留痕"


def test_drawdown_none_allows(el, monkeypatch):
    """净值不可算 (无持仓) → 放行 (不造假不触发)"""
    g = _gate(el)
    monkeypatch.setattr(g, "_current_drawdown", lambda: None)
    can, _ = g.check("000001", "14:00")
    assert can


# -- 接线守卫 -------------------------------------------------------------------

def test_entry_engine_wires_protection_gate():
    """check_entry 源码必须含 ProtectionGate 前置调用"""
    import inspect
    from execution_layer.core.entry_engine import EntryEngine
    src_code = inspect.getsource(EntryEngine.check_entry)
    assert "ProtectionGate" in src_code, "风控闸门未接入 entry_engine"


def test_clean_ledger_allows(el):
    """空账本 → 三守卫全过"""
    can, reason = _gate(el).check("000001", "14:00")
    assert can, f"空账本应放行: {reason}"
