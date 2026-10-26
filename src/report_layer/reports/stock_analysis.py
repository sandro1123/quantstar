"""report_layer/reports/stock_analysis.py — StockAnalysisBuilder"""

from delivery_layer.message import Message
from report_layer.reports.base import ReportBuilder, ReportContext
from agent_layer import build_request


class StockAnalysisBuilder(ReportBuilder):
    content_type = "stock_analysis"
    title = "个股深度分析"

    def __init__(self, code: str):
        self.code = code
        self._sections = []

    def sections(self) -> list:
        return self._sections

    def build(self, context: ReportContext) -> Message | None:
        agent = context.agent_layer
        if agent is None:
            return None
        req = build_request(task="stock_analysis", input={"code": self.code, "date": context.date}, consumer="report")
        response = agent.analyze(req)
        content = {
            "code": self.code,
            "date": context.date,
            "verdict": response.verdict,
            "confidence": response.confidence,
            "reasoning": response.reasoning,
            "risk_flags": [f.flag for f in response.risk_flags],
        }
        return Message(
            content=content,
            content_type=self.content_type,
            title=f"{self.title}: {self.code}",
        )
