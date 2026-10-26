"""Q5 防回归: IC target_date 交易日滞后解析的日期边界 (增强, 非修红)。

背景: test_ic_feedback_horizon 的 ×2 红已随 ST-31 (93d5fc2) handler 侧
修复自愈 (销案定性, 非 fixture 修复)。本件补防回归边界用例, 钉住
_resolve_ic_target_date / TradingCalendar 的周末与节假日语义, 防止
未来改回自然日减法 (旧 bug: 周末/节假日落非交易日 → factor_daily 无行
→ IC 环静默跳过)。

统筹裁定 (sm-27c8ff9ab1bf40bd): 小件单 commit 落 dao4-budget-unify。
"""
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from common.calendar import TradingCalendar
from ops_layer import orchestrator as orch_mod


# ---- TradingCalendar 退化路径 (无 db: 周末 + 内置节假日判断) ----

class TestCalendarFallback:

    def test_weekend_not_trading_day(self):
        cal = TradingCalendar(db=None)
        assert cal.is_trading_day("2026-08-22") is False   # 周六
        assert cal.is_trading_day("2026-08-23") is False   # 周日
        assert cal.is_trading_day("2026-08-21") is True    # 周五

    def test_builtin_holiday_not_trading_day(self):
        cal = TradingCalendar(db=None)
        assert cal.is_trading_day("2026-10-01") is False   # 国庆
        assert cal.is_trading_day("2026-05-01") is False   # 劳动节

    def test_prev_trading_day_skips_weekend(self):
        """周一往回数 1 个交易日 = 上周五 (不是周日)。"""
        cal = TradingCalendar(db=None)
        assert cal.prev_trading_day("2026-08-24") == "2026-08-21"

    def test_prev_trading_day_skips_holiday_block(self):
        """劳动节后首个工作日往回数 1 个交易日 = 节前最后交易日 04-30。"""
        cal = TradingCalendar(db=None)
        assert cal.prev_trading_day("2026-05-06") == "2026-04-30"

    def test_prev_trading_day_from_weekday(self):
        cal = TradingCalendar(db=None)
        assert cal.prev_trading_day("2026-08-20") == "2026-08-19"


# ---- _resolve_ic_target_date: 交易日滞后 (monkeypatch bj_date 钉住运行日) ----

class TestResolveICTargetDate:

    def _resolve(self, monkeypatch, today, lag):
        monkeypatch.setattr(orch_mod, "bj_date", lambda: today)
        return orch_mod._resolve_ic_target_date(None, lag)

    def test_lag_from_monday_lands_on_friday(self, monkeypatch):
        """运行日=周一, lag=1 → 上周五 (旧自然日实现会落周日 → 无行静默跳过)。"""
        assert self._resolve(monkeypatch, "2026-08-24", 1) == "2026-08-21"

    def test_lag_counts_trading_days_not_calendar_days(self, monkeypatch):
        """lag=3 从周一往回数 3 个交易日 = 上周三 (跨过周末)。"""
        assert self._resolve(monkeypatch, "2026-08-24", 3) == "2026-08-19"

    def test_lag_across_holiday_block(self, monkeypatch):
        """运行日=劳动节后首个工作日, lag=1 → 节前最后交易日。"""
        assert self._resolve(monkeypatch, "2026-05-06", 1) == "2026-04-30"

    def test_lag_zero_returns_today(self, monkeypatch):
        assert self._resolve(monkeypatch, "2026-08-21", 0) == "2026-08-21"

    def test_result_is_always_trading_day(self, monkeypatch):
        """任意 lag 的解析结果本身必须是交易日 (退化路径)。"""
        cal = TradingCalendar(db=None)
        for lag in range(0, 8):
            d = self._resolve(monkeypatch, "2026-08-24", lag)
            assert cal.is_trading_day(d), f"lag={lag} -> {d} 非交易日"
