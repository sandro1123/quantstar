"""Issue #14: TradingCalendar 2026 年 fallback 节假日修正。

_build_holidays 节假日表曾只覆盖到 2026-10-08，导致 db 边界之后（静态 fallback 路径）
把 2026 年剩余节假日（中秋 9-25）和 2027 年节假日（元旦 1-1、春节 2-11~2-17）
错判为交易日，next_trading_day / valid_until 会落在节假日。
"""
from common.calendar import TradingCalendar


def _cal():
    return TradingCalendar()  # 纯静态 fallback，不读 db


def test_2026_mid_autumn_not_trading():
    """2026-09-25 中秋（周五）应非交易日，补全前会误判为真。"""
    cal = _cal()
    assert cal.is_trading_day("2026-09-25") is False


def test_2026_after_mid_autumn_trading():
    """中秋后第一个周一 2026-09-28 应为交易日（误判会把它当连续假期）。"""
    cal = _cal()
    assert cal.is_trading_day("2026-09-28") is True


def test_2027_new_year_not_trading():
    """2027-01-01 元旦（周五）应非交易日，补全前会误判为真。"""
    cal = _cal()
    assert cal.is_trading_day("2027-01-01") is False


def test_2027_spring_festival_not_trading():
    """2027-02-11~2027-02-17 春节长假均应非交易日。"""
    cal = _cal()
    for d in ["2027-02-11", "2027-02-12", "2027-02-13", "2027-02-14",
              "2027-02-15", "2027-02-16", "2027-02-17"]:
        assert cal.is_trading_day(d) is False, f"{d} 应为春节假期非交易日"


def test_2027_after_spring_festival_trading():
    """春节后第一个工作日 2027-02-18（周四）应为交易日。"""
    cal = _cal()
    assert cal.is_trading_day("2027-02-18") is True


def test_next_trading_day_from_2026_10_08_holiday():
    """国庆最后一天 2026-10-08（周四）的下一个交易日应跳过周末到 2026-10-09（周五）。"""
    cal = _cal()
    assert cal.next_trading_day("2026-10-08") == "2026-10-09"


def test_next_trading_day_crosses_2027_new_year():
    """2026-12-31（周四，元旦前）下一交易日应跳过元旦和周末到 2027-01-04（周一）。"""
    cal = _cal()
    assert cal.next_trading_day("2026-12-31") == "2027-01-04"


def test_next_trading_day_crosses_2027_spring_festival():
    """2027-02-10（周三，春节前）下一交易日应跳过春节长假到 2027-02-18（周四）。"""
    cal = _cal()
    assert cal.next_trading_day("2027-02-10") == "2027-02-18"


def test_entry_valid_until_not_landing_on_holiday():
    """entry_valid_until 5 交易日窗口不应落在节假日（与 plan_manager 交叉验证）。"""
    cal = _cal()
    # 2026-09-24 周四挂入，跳过中秋+周末，下一交易日应为 2026-09-28 周一
    d = cal.next_trading_day("2026-09-24")
    assert d == "2026-09-28", d   # 跳过中秋+周末


# ---------------------------------------------------------------------------
# 变异验证 (AGENTS.md): 移除任一节假日，对应断言必须报红
# ---------------------------------------------------------------------------

from tests.unit._mutation import assert_mutation_caught


def test_mutation_mid_autumn_holiday_removed_is_caught():
    """移除 2026-09-25 中秋节假日 → 中秋会被误判为交易日，断言必须报红。"""
    from common import calendar as cal_mod
    orig = cal_mod.TradingCalendar._build_holidays

    def _mutate():
        cal_mod.TradingCalendar._build_holidays = lambda self: orig(self) - {"2026-09-25"}

    def _check():
        cal = TradingCalendar()
        assert cal.is_trading_day("2026-09-25") is False, "中秋应非交易日"

    try:
        assert_mutation_caught(_mutate, _check, "中秋节假日移除后中秋非交易日断言")
    finally:
        cal_mod.TradingCalendar._build_holidays = orig


def test_mutation_2027_spring_festival_removed_is_caught():
    """移除 2027-02-11 春节节假日 → 春节会被误判为交易日，断言必须报红。"""
    from common import calendar as cal_mod
    orig = cal_mod.TradingCalendar._build_holidays

    def _mutate():
        cal_mod.TradingCalendar._build_holidays = lambda self: orig(self) - {"2027-02-11"}

    def _check():
        cal = TradingCalendar()
        assert cal.is_trading_day("2027-02-11") is False, "春节应非交易日"

    try:
        assert_mutation_caught(_mutate, _check, "春节节假日移除后春节非交易日断言")
    finally:
        cal_mod.TradingCalendar._build_holidays = orig


