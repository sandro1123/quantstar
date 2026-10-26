"""BT-01: PortfolioMode 崩溃修复回归测试

B-01: modes/portfolio.py one_line_verdict 用 metrics['sharpe'] 对 BacktestMetrics
dataclass 做下标访问, mode="portfolio" 一跑必抛 TypeError; 且此前全 tests 目录
对 PortfolioMode 零覆盖, 崩溃路径无测试。本文件补上:
- 完整跑通路径 (含 one_line_verdict 构造)
- 无可成交标的时的空报告路径
"""
from types import SimpleNamespace

from backtest_layer.core.broker import Broker
from backtest_layer.core.models import BacktestInput, CostConfig
from backtest_layer.modes.portfolio import PortfolioMode


class FakeDataLayer:
    """按日期返回固定 kline 快照的假数据层"""

    def __init__(self, closes: dict[str, float]):
        self._closes = closes

    def snapshot(self, date: str):
        klines = [SimpleNamespace(code=code, close=price, open=price)
                  for code, price in self._closes.items()]
        return SimpleNamespace(klines=klines)


def _input(codes: list[str]) -> BacktestInput:
    return BacktestInput(mode="portfolio", codes=codes,
                         date_range=("2026-08-03", "2026-08-07"))


def test_portfolio_mode_runs_and_builds_verdict():
    """崩溃路径回归: one_line_verdict 构造不再抛 TypeError"""
    # 资金留足手续费余量, 避免 insufficient_funds 走空报告路径
    broker = Broker(CostConfig(initial_capital=105_500.0, slippage_rate=0.0))
    mode = PortfolioMode(broker, FakeDataLayer({"600519": 10.0}), strategy_layer=None)
    report = mode.run(_input(["600519"]))

    assert report.mode == "portfolio"
    assert report.total_trades == 1
    assert report.one_line_verdict.startswith("组合回测: 1只")
    assert "夏普" in report.one_line_verdict
    # 买入 1000 股 @10, 卖出同价 → 收益≈-交易成本, 不应为正假值
    assert report.total_return <= 0.0


def test_portfolio_mode_multi_stock():
    broker = Broker(CostConfig(initial_capital=211_000.0, slippage_rate=0.0))
    mode = PortfolioMode(broker,
                         FakeDataLayer({"600519": 10.0, "000001": 20.0}),
                         strategy_layer=None)
    report = mode.run(_input(["600519", "000001"]))
    assert report.total_trades == 2
    assert "2只" in report.one_line_verdict


def test_portfolio_mode_no_fillable_returns_empty_report():
    """价格过高买不起一手 → 空报告, 不崩不造假"""
    broker = Broker(CostConfig(initial_capital=1_000.0, slippage_rate=0.0))
    mode = PortfolioMode(broker, FakeDataLayer({"600519": 1000.0}), strategy_layer=None)
    report = mode.run(_input(["600519"]))
    assert report.total_trades == 0
    assert report.total_return == 0.0
    assert report.one_line_verdict == ""
