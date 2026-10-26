# -*- coding: utf-8 -*-
"""B2 补钉 (W1 复核): 守卫必须真走 TradingCalendar 路径。

问题: 原三钉中钉(1)(2) 都 monkeypatch 掉 `_is_trading_day` 直接返 True/False,
因此它们**只证明 run() 的分支逻辑**, 不证明 `TradingCalendar(db=db)` 这条
真实路径能返回 False。若真实调用写错(如签名/参数名), 三钉仍全绿 = 守卫空转。

本钉用真 TradingCalendar 静态日历 (10-01/10-05/10-07 是法定假日,
10-20 是交易日), 不 monkeypatch 任何东西, 直接验 `_is_trading_day` 真值。
"""
import importlib.util
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))


def _load():
    spec = importlib.util.spec_from_file_location(
        "e2e_verification_b2_real",
        os.path.join(ROOT, "scripts", "e2e_verification.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load()


def test_real_calendar_path_detects_holidays_and_trading_days():
    """真日历: 10-01/05/07 判非交易日, 10-20 判交易日。"""
    for holiday in ("2026-10-01", "2026-10-05", "2026-10-07"):
        assert MOD._is_trading_day(holiday) is False, (
            f"{holiday} 是法定��市日, 守卫应判非交易日")
    for trading in ("2026-10-20", "2026-10-21"):
        assert MOD._is_trading_day(trading) is True, (
            f"{trading} 是交易日, 守卫不得误伤")


def test_guard_uses_trading_calendar_not_hardcoded():
    """守卫必须引用 TradingCalendar (静态检查, 防有人换成硬编码日期表)。"""
    import inspect
    src = inspect.getsource(MOD._is_trading_day)
    assert "TradingCalendar" in src, (
        "守卫未使用 TradingCalendar —— 换成硬编码日期表会漂移")
