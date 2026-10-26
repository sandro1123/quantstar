# -*- coding: utf-8 -*-
"""盘前速报 (宪法 #7, 09:25) — 集合竞价阶段扫描昨日推荐+观察池。

两段: 竞价快评 (逐票, 缺数据诚实降级) + 操作指引收尾。
"""
from report_layer.reports.base import ReportBuilder
from report_layer.sections.premarket_review import PremarketReviewSection
from report_layer.sections.risk_compliance import RiskComplianceSection


class PremarketBriefBuilder(ReportBuilder):
    @property
    def content_type(self) -> str:
        return "premarket_brief"

    @property
    def title(self) -> str:
        return "⚡ QuantStar 盘前速报"

    def sections(self) -> list:
        return [
            PremarketReviewSection(),   # ⚡ 竞价快评 (置顶)
            RiskComplianceSection(),    # ⚠️ 风险提示 + 合规固定段
        ]
