# -*- coding: utf-8 -*-
"""RED: dev_verify 轻量化切片 section — 必须不触 run_daily, 只读核心表新鲜度。"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from unittest.mock import MagicMock, patch


def _ctx():
    from report_layer.reports.base import ReportContext
    ctx = ReportContext(date="2026-09-01", report_mode="debug")
    ctx.data_layer = MagicMock()
    return ctx


def test_lightweight_section_exists():
    from report_layer.reports.dev_verify import DevLightweightSection
    s = DevLightweightSection()
    assert s.name == "dev_lightweight"


def test_lightweight_never_calls_run_daily():
    """核心红线: 新 section 全程零 run_daily 调用 (60s 超时根因即此镜像链)。"""
    from report_layer.reports.dev_verify import DevLightweightSection
    ctx = _ctx()
    strategy = MagicMock()
    ctx.strategy_layer = strategy
    conn = MagicMock()

    def fake_execute(sql, *a):
        m = MagicMock()
        m.fetchone.return_value = (5546, "2026-09-01")
        return m

    conn.execute.side_effect = fake_execute
    ctx.data_layer.get_connection.return_value = conn
    s = DevLightweightSection()
    s.build(ctx)
    assert not strategy.run_daily.called, "dev_verify 轻量切片必须不调 run_daily"


def test_lightweight_reads_core_tables():
    """数据面: factor/kline/index/valuation 四核心 MAX(date)+COUNT 必须出报告体。"""
    from report_layer.reports.dev_verify import DevLightweightSection
    ctx = _ctx()
    conn = MagicMock()
    ctx.data_layer.get_connection.return_value = conn

    def fake_execute(sql, *a):
        sql_l = sql.lower()
        m = MagicMock()
        if "max(date)" in sql_l and "count(*)" in sql_l:
            m.fetchone.return_value = (5546, "2026-09-01")
        else:
            m.fetchone.return_value = (5546,)
        return m

    conn.execute.side_effect = fake_execute
    s = DevLightweightSection()
    r = s.build(ctx)
    assert r is not None
    body = r.body
    assert "2026-09-01" in body and "5546" in body


def test_lightweight_freshness_degraded_surface():
    """降级面: 核心表缺行/MAX 落后必须显式红字, 不静默。"""
    from report_layer.reports.dev_verify import DevLightweightSection
    ctx = _ctx()
    conn = MagicMock()
    ctx.data_layer.get_connection.return_value = conn

    def fake_execute(sql, *a):
        m = MagicMock()
        if "max(date)" in sql.lower() and "count(*)" in sql.lower():
            m.fetchone.return_value = (0, "2026-08-31")  # 零行+落后一天
        else:
            m.fetchone.return_value = (0,)
        return m

    conn.execute.side_effect = fake_execute
    s = DevLightweightSection()
    r = s.build(ctx)
    assert r is not None
    assert "⚠" in r.body or "缺" in r.body or "落后" in r.body


def test_builder_sections_swapped():
    """DevVerifyBuilder 编排换新 section; 旧 DevCompareSection 类保留(存量测试仍绿)。"""
    from report_layer.reports.dev_verify import DevVerifyBuilder, DevLightweightSection
    b = DevVerifyBuilder()
    names = [type(s).__name__ for s in b.sections()]
    assert "DevLightweightSection" in names
    assert "DevCompareSection" not in names
