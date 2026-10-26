"""M2: 风控三件套闸门 — 冷却期/连损日停/回撤熔断(迟滞带) + 接线守卫"""
from datetime import date, timedelta
from common.utils import bj_date

import pytest

from common.database import Database
from execution_layer import ExecutionLayer
from execution_layer.core.protection_gate import ProtectionGate
from tests.unit._mutation import assert_mutation_caught


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


# -- Issue #9: fail-closed when a guard throws ----------------------------

def _make_guard_boom(el, guard_name: str):
    """把指定守卫注入异常，验证 check() 熔断拦截（不诚实放行）。"""
    g = _gate(el)
    orig = getattr(g, guard_name)
    def _boom(code):
        raise RuntimeError(f"guard {guard_name} 依赖失效")
    setattr(g, guard_name, _boom)
    return g, orig


def test_guard_exception_fail_closed_cooldown(el):
    """冷却期守卫抛异常 → check() 必须拦截 (fail-closed，Issue #9)。"""
    g, orig = _make_guard_boom(el, "_check_cooldown")
    can, reason = g.check("000001", "14:00")
    assert not can, f"守卫异常时必须拦截: {reason}"
    assert "fail-closed" in reason or "熔断拦截" in reason or "异常" in reason
    # 留痕必须落库
    rows = el.db.get_connection().execute(
        "SELECT reason FROM action_records WHERE code='000001' "
        "AND action_type='skip_entry'").fetchall()
    assert any("fail-closed" in str(r["reason"]) or "熔断拦截" in str(r["reason"])
               for r in rows), "拦截须留痕"


def test_guard_exception_fail_closed_stoploss(el):
    """连损日停守卫抛异常 → check() 必须拦截 (fail-closed，Issue #9)。"""
    g, orig = _make_guard_boom(el, "_check_daily_stoploss")
    can, reason = g.check("000001", "14:00")
    assert not can, f"守卫异常时必须拦截: {reason}"
    assert "异常" in reason or "fail-closed" in reason or "熔断拦截" in reason


def test_guard_exception_fail_closed_drawdown(el):
    """回撤熔断守卫抛异常 → check() 必须拦截 (fail-closed，Issue #9)。"""
    g, orig = _make_guard_boom(el, "_check_drawdown")
    can, reason = g.check("000001", "14:00")
    assert not can, f"守卫异常时必须拦截: {reason}"
    assert "异常" in reason or "fail-closed" in reason or "熔断拦截" in reason


def test_guard_exception_then_recovery_allows(el):
    """守卫恢复正常后，check() 再次放行 (fail-closed 不是永久拒止)。"""
    g = _gate(el)
    # 首次：cooldown 抛异常 → 拦截
    boom = lambda code: (_ for _ in ()).throw(RuntimeError("依赖失效"))
    orig_cooldown = g._check_cooldown
    g._check_cooldown = boom
    can, _ = g.check("000001", "14:00")
    assert not can, "异常时应拦截"
    # 恢复：放行
    g._check_cooldown = orig_cooldown
    can, reason = g.check("000001", "14:00")
    assert can, f"恢复后应放行: {reason}"


# -- 变异验证 (AGENTS.md 断言必须证明自己会红) -------------

def test_mutation_fail_closed_is_real(el):
    """变异验证：若 ProtectionGate.check 回退到旧 fail-open (守卫异常 continue 放行),
    断言必须报红，否则永绿断言等于没有断言 (AGENTS.md 铁律 #1)。"""
    from execution_layer.core.protection_gate import ProtectionGate

    g = _gate(el)
    orig_check = ProtectionGate.check

    def _mutate_to_fail_open():
        def _fail_open_check(self, code, current_time):
            for guard in (self._check_cooldown, self._check_daily_stoploss,
                          self._check_drawdown):
                try:
                    reason = guard(code)
                except Exception:
                    continue  # 旧实现：守卫故障放行 (fail-open)
                if reason:
                    self._record_block(code, reason)
                    return False, reason
            return True, ""
        ProtectionGate.check = _fail_open_check

    def _check_blocks_on_guard_exception():
        boom = lambda code: (_ for _ in ()).throw(RuntimeError("依赖失效"))
        orig = g._check_cooldown
        g._check_cooldown = boom
        try:
            can, _ = g.check("000001", "14:00")
        finally:
            g._check_cooldown = orig
        assert not can, "守卫异常时必须拦截 (fail-closed)"

    try:
        assert_mutation_caught(_mutate_to_fail_open,
                               _check_blocks_on_guard_exception,
                               "protection gate fail-closed (守卫异常须拦截)")
    finally:
        ProtectionGate.check = orig_check
