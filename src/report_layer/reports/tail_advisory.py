"""尾盘投顾 — 14:52 独立管道 (卡VII, 统筹裁定 sm-c525b45463d94435 裁定③)

午后实时视角: 只读看候选/持仓现况, 供尾盘决策参考。
独立管道 (非晨/午既有管线 — 时点互斥), 同链 report_layer build+push,
qscard 不动 (留给盘中触价卡)。

只读铁律: 全程零写库 (advisory_brief 函数层保证)。
"""
from report_layer.reports.base import ReportBuilder
from report_layer.sections.advisory_brief import AdvisoryTailSection


class TailAdvisoryBuilder(ReportBuilder):
    @property
    def content_type(self) -> str:
        return "tail_advisory"

    @property
    def title(self) -> str:
        return "🧭 QuantStar 尾盘投顾"

    def sections(self) -> list:
        return [
            AdvisoryTailSection(),
        ]
