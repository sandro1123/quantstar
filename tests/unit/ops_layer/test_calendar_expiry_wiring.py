# -*- coding: utf-8 -*-
"""#14-b 接线验证 — 日历过期告警必须真的从 health_check 发出

判例 #150「守卫必须跑真路径」: 光有 TradingCalendar.static_table_warning
这个纯函数不算通电 —— 必须证明 ops health_check 真的会调它并落 alerts。

本卡用**假 alert_manager** 捕获 send 调用, 断言:
  1. 表过期时 → 真发一条 warning alert (title/detail/action 齐);
  2. 未过期时 → 不发 (不打扰);
  3. 告警发送失败 → 不阻断健康检查 (fail-safe)。
"""
from unittest.mock import Mock

import pytest

from common.calendar import TradingCalendar
from ops_layer.orchestrator import PipelineOrchestrator


def _orch():
    """构造最小 orchestrator — 只测 _check_calendar_expiry, 不需要完整栈。"""
    o = PipelineOrchestrator.__new__(PipelineOrchestrator)
    o.alert_manager = Mock()
    return o


class TestCalendarExpiryWiring:
    def test_sends_alert_when_table_expired(self, monkeypatch):
        """表过期 → 真发一条 warning 告警"""
        o = _orch()
        monkeypatch.setattr(
            TradingCalendar, "static_table_warning",
            staticmethod(lambda d, lead_days=90: "交易日历静态表已过期 99 天 — 测试文案"))

        msg = o._check_calendar_expiry()

        assert msg is not None
        assert o.alert_manager.send.call_count == 1, "必须真发告警, 不能只算不报"
        alert = o.alert_manager.send.call_args[0][0]
        assert alert.severity == "warning"
        assert "日历" in alert.title
        assert "99 天" in alert.detail
        assert "_STATIC_COVERAGE_END" in alert.action, "action 须给出具体修法"

    def test_no_alert_when_table_fresh(self, monkeypatch):
        """表还新 → 不发告警 (不打扰)"""
        o = _orch()
        monkeypatch.setattr(
            TradingCalendar, "static_table_warning",
            staticmethod(lambda d, lead_days=90: None))

        assert o._check_calendar_expiry() is None
        assert o.alert_manager.send.call_count == 0

    def test_approaching_expiry_uses_distinct_title(self, monkeypatch):
        """临近过期 vs 已过期 → 标题须可区分 (运维一眼看出紧急度)"""
        o = _orch()
        monkeypatch.setattr(
            TradingCalendar, "static_table_warning",
            staticmethod(lambda d, lead_days=90: "交易日历静态表将于 2027-12-31 到期 (剩 30 天)"))

        o._check_calendar_expiry()
        alert = o.alert_manager.send.call_args[0][0]
        assert "临近过期" in alert.title, "临近与已过期必须不同标题"

    def test_alert_failure_does_not_raise(self, monkeypatch):
        """告警发送炸了 → 不向上抛 (健康检查不能被附加面拖垮)"""
        o = _orch()
        o.alert_manager.send.side_effect = RuntimeError("delivery down")
        monkeypatch.setattr(
            TradingCalendar, "static_table_warning",
            staticmethod(lambda d, lead_days=90: "已过期 1 天"))

        msg = o._check_calendar_expiry()   # 不应抛
        assert msg is not None, "告警失败仍须返回判据结果 (不静默)"

    def test_real_calendar_not_expired_today(self):
        """真实类: 当前时点 (2026-10-29) 不该报过期 — 防夹具与真值脱节"""
        assert TradingCalendar.static_table_warning("2026-10-29") is None
