"""可插拔分析器 — BaseAnalyzer + Registry

BT-11 ⑥ (B-11 排队态标注): 本模块当前零生产消费者 (无 register 调用方,
run_all 无人调用)。按 D-18 标排队态, 保留代码不删。
启用条件: ① 有真实分析器实现 (如归因/风格分析) 注册进 AnalyzerRegistry;
② engine.run() 或 CLI/报告链路消费 AnalysisReport;
③ 补生产调用方的单测。满足前本模块仅被单测覆盖。
"""

from abc import ABC, abstractmethod

from backtest_layer._models import AnalysisReport
from backtest_layer.core.models import BacktestReport


class BaseAnalyzer(ABC):
    @abstractmethod
    def analyze(self, report: BacktestReport) -> dict:
        ...


class AnalyzerRegistry:
    def __init__(self):
        self._analyzers: dict[str, BaseAnalyzer] = {}

    def register(self, name: str, analyzer: BaseAnalyzer):
        self._analyzers[name] = analyzer

    def run_all(self, report: BacktestReport) -> AnalysisReport:
        results = {}
        for name, analyzer in self._analyzers.items():
            try:
                results[name] = analyzer.analyze(report)
            except Exception as e:
                results[name] = {"error": str(e)}
        return AnalysisReport(analyzers=results)

    def get(self, name: str) -> BaseAnalyzer | None:
        return self._analyzers.get(name)


def auto_pair_trades(trade_log: list[dict]) -> list[dict]:
    """自动配对入场/出场交易"""
    buys = [t for t in trade_log if t.get("action") in ("buy", "entry")]
    sells = [t for t in trade_log if t.get("action") in ("sell", "exit")]
    pairs = []
    for sell in sells:
        code = sell.get("code", "")
        matching = [b for b in buys if b.get("code") == code]
        if matching:
            buy = matching[-1]
            pairs.append({
                "code": code,
                "entry_date": buy.get("date", ""),
                "exit_date": sell.get("date", ""),
                "entry_price": buy.get("price", 0),
                "exit_price": sell.get("price", 0),
                "shares": buy.get("shares", 0),
                "pnl": sell.get("pnl", 0),
                "holding_days": 0,
            })
    return pairs
