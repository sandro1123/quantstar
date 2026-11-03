# -*- coding: utf-8 -*-
"""#14-b 交易日历静态表过期告警 — RED 优先

Issue #14 深层要求 (原文):
  「2027 起或数据源失败时的 fallback 行为需明确, 至少应在表过期时告警
    而非静默当作交易日。」

P0 只做了「多填两年节假日」, 没有告警机制 → 表末之后会再次静默失灵。
本卡补判据层 (纯函数, 不直接发告警; 接线在 ops 健康检查)。

纪律: 每条断言须能报红 (AGENTS.md §1) — 用 _mutation 工具做反向验证。
注意: _mutation 工具**不还原**变异, 故变异验证一律放在测试末尾,
      或对纯函数用独立副本注入, 避免污染同文件其它用例。
"""
from datetime import datetime, timedelta

import pytest

from common.calendar import TradingCalendar

from tests.unit._mutation import assert_mutation_caught


def _shift(end: str, days: int) -> str:
    return (datetime.strptime(end, "%Y-%m-%d") + timedelta(days=days)).strftime("%Y-%m-%d")


# ============================================================ 组1: 覆盖率标记
class TestCoverageMarker:
    def test_static_table_end_is_exposed(self):
        """表末日期可被外部读取 (供 ops 健康检查使用)"""
        end = TradingCalendar.static_table_end()
        assert isinstance(end, str)
        assert len(end) == 10 and end[4] == "-" and end[7] == "-"

    def test_coverage_marker_covers_every_registered_holiday(self):
        """覆盖末日必须 >= 表中登记的最晚节假日。

        反向钉: 若有人往 _build_holidays 加了更晚的节日却忘了改
        _STATIC_COVERAGE_END, 覆盖标记就会低估实际覆盖。
        """
        cal = TradingCalendar()
        latest_holiday = max(cal._holidays)
        assert latest_holiday <= TradingCalendar.static_table_end(), (
            f"_build_holidays 最晚节日 {latest_holiday} 超出覆盖标记 "
            f"{TradingCalendar.static_table_end()} — 两处必须同步更新")


# ============================================================ 组2: 过期天数
class TestStaleBy:
    def test_not_stale_inside_table(self):
        """表内日期 → 负数或 0 (未过期)"""
        assert TradingCalendar.static_table_stale_by("2026-10-29") <= 0

    def test_not_stale_on_end_date(self):
        """边界: 恰在表末日 → 0, 不算过期"""
        assert TradingCalendar.static_table_stale_by(TradingCalendar.static_table_end()) == 0

    def test_stale_after_end(self):
        """表末之后 → 正数 (已过期)"""
        assert TradingCalendar.static_table_stale_by("2028-03-01") > 0

    def test_stale_days_are_calendar_days(self):
        """天数语义 = 自然日差 — 告警只需量级正确"""
        assert TradingCalendar.static_table_stale_by(
            _shift(TradingCalendar.static_table_end(), 10)) == 10


# ============================================================ 组3: 告警文案
class TestWarning:
    def test_no_warning_when_far_from_end(self):
        """远离表末 → None (不打扰)"""
        far = _shift(TradingCalendar.static_table_end(), -200)
        assert TradingCalendar.static_table_warning(far, lead_days=90) is None

    def test_warning_when_expired(self):
        """已过期 → 有文案且明说后果"""
        msg = TradingCalendar.static_table_warning("2028-03-01")
        assert msg is not None
        assert "过期" in msg and "交易日" in msg

    def test_warning_when_within_lead_days(self):
        """临近 (<= lead_days) → 提前提醒"""
        near = _shift(TradingCalendar.static_table_end(), -30)
        msg = TradingCalendar.static_table_warning(near, lead_days=90)
        assert msg is not None and "到期" in msg

    def test_no_warning_just_outside_lead_window(self):
        """刚好超出提醒窗 (lead_days+1 天) → None, 防过早噪音"""
        far = _shift(TradingCalendar.static_table_end(), -91)
        assert TradingCalendar.static_table_warning(far, lead_days=90) is None

    def test_warning_is_none_or_str(self):
        """三态诚实: 只有 None (正常) 与 non-empty str (告警), 不用空串冒充"""
        for d in ("2026-10-29", "2027-12-31", "2028-06-01"):
            w = TradingCalendar.static_table_warning(d)
            assert w is None or (isinstance(w, str) and w.strip())


# ============================================================ 组4: 不误伤现有行为
class TestNoRegression:
    @pytest.mark.parametrize("d,expected", [
        ("2026-10-29", True),    # 普通周四
        ("2026-10-01", False),   # 国庆
        ("2026-10-31", False),   # 周六
        ("2026-09-25", False),   # 中秋
    ])
    def test_static_trading_day_unchanged(self, d, expected):
        """新增判据不得改变既有交易日判断"""
        assert TradingCalendar()._static_is_trading_day(d) is expected


# ============================================================ 组5: 变异验证
#
# 注意三点 (都是实测踩过的坑):
# 1. assert_mutation_caught **不还原**变异 → 只动实例, 不动类 (否则污染同文件);
# 2. check 必须是**会断言**的可调用 —— 若只返回 bool 而不 assert, 它永远不抛
#    AssertionError → 工具会判「永绿」。这坑我第一次就踩了。
# 3. mutate/check 都在工具内部作用域执行, 闭包变量最稳。
class TestMutation:
    def test_mutation_stale_by_always_zero(self):
        """变异: stale_by 恒 0 (永不告警) → 断言必须报红"""
        cal = TradingCalendar()

        def mutate():
            cal.static_table_stale_by = lambda d: 0

        def check():
            assert cal.static_table_stale_by("2028-03-01") > 0

        assert_mutation_caught(mutate, check, "过期天数判据")

    def test_mutation_warning_always_none(self):
        """变异: 告警函数恒 None (永不提醒) → 断言必须报红"""
        cal = TradingCalendar()

        def mutate():
            cal.static_table_warning = lambda d, lead_days=90: None

        def check():
            assert cal.static_table_warning("2028-03-01") is not None

        assert_mutation_caught(mutate, check, "过期告警")

    def test_class_untouched_after_mutations(self):
        """钉: 变异只动实例, 类方法必须完好 (上一版正是污染了全局类)"""
        assert TradingCalendar.static_table_stale_by("2028-03-01") > 0
        assert TradingCalendar.static_table_warning("2028-03-01") is not None
