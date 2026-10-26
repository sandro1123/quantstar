"""新报告类型单元测试"""
import pytest
from report_layer.reports.morning_brief import MorningBriefBuilder
from report_layer.reports.midday_brief import MiddayBriefBuilder
from report_layer.reports.weekly_summary import WeeklySummaryBuilder
from report_layer.reports.base import ReportContext


def test_morning_brief_builder():
    b = MorningBriefBuilder()
    assert b.content_type == "morning_brief"
    # R1 (a6e69d7) 七段结构 + 卡VII (c5770a7) AdvisoryMorningSection
    # = 8 段 (news/indices 已移出, 旧 F5.1 十段编排作废)。段序权威断言见
    # tests/unit/report_layer/test_r1_morning_brief.py (TestR1BuilderComposition)。
    assert len(b.sections()) == 8


def test_midday_brief_builder():
    b = MiddayBriefBuilder()
    assert b.content_type == "midday_brief"
    # R3 (dc5abff) 6 段 + 卡VII (c5770a7) AdvisoryMiddaySection = 7 段
    # (S5 五段编排作废)。段序见 src/report_layer/reports/midday_brief.py。
    assert len(b.sections()) == 7


def test_weekly_summary_builder():
    b = WeeklySummaryBuilder()
    assert b.content_type == "weekly_summary"
    assert len(b.sections()) == 3  # P0-6/7: Portfolio + ActionSummary + WeeklyReview


def test_morning_brief_build(monkeypatch):
    from report_layer.sections.overnight_market import OvernightMarketSection
    monkeypatch.setattr(OvernightMarketSection, "_fetch_overnight", lambda self: {})
    b = MorningBriefBuilder()
    ctx = ReportContext(date="2026-07-17")
    msg = b.build(ctx)
    # 可能返回 None（无数据层）
    # 但不应该抛异常
    assert msg is None or msg.content_type == "morning_brief"
