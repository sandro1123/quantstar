"""backtest_layer/modes/portfolio.py — 组合回测（共享资金池）"""

from backtest_layer.core.broker import Broker
from backtest_layer.core.metrics import Metrics
from backtest_layer.core.models import BacktestInput, BacktestReport
from backtest_layer.core.sizer import AllInSizer
from backtest_layer.modes.hold import cost_summary, simulate_buy_and_hold


class PortfolioMode:
    def __init__(self, broker: Broker, data_layer, strategy_layer):
        self.broker = broker
        self.data_layer = data_layer
        self.strategy_layer = strategy_layer
        # BT-11 ①: 仓位计算接 AllInSizer (资金等分, 100 股取整) — 替代内联算式
        self._sizer = AllInSizer()

    def run(self, input: BacktestInput) -> BacktestReport:
        broker = self.broker
        sim = simulate_buy_and_hold(broker, self.data_layer, input, self._sizer)
        trade_log = sim["trade_log"]
        equity_curve = sim["equity_curve"]

        if not sim["buy_results"]:
            return BacktestReport(
                mode="portfolio", date_range=input.date_range,
                total_return=0.0, annual_return=0.0, sharpe=0.0,
                max_drawdown=0.0, win_rate=0.0, total_trades=0,
            )

        # BT-CORRECTNESS ②: 收盘 NAV 未卖出持仓按最后已知价估值 (原 nav({}) 按 0 计)
        nav = sim["nav"]
        nav_series = [e["nav"] for e in equity_curve]
        # BT-06: SQN 用每笔交易 pnl 真算
        metrics = Metrics.calc(nav_series,
                               trade_pnls=[t["pnl"] for t in trade_log] or None)
        total_return = (nav / broker.cost.initial_capital - 1) if broker.cost.initial_capital > 0 else 0.0
        winning_trades = [t for t in trade_log if t["pnl"] > 0]
        win_rate = len(winning_trades) / len(trade_log) if trade_log else 0.0
        pre_cost_return, cost_impact = cost_summary(trade_log, nav, broker.cost.initial_capital)

        pf = Metrics.profit_factor(trade_log)
        exp = Metrics.expectancy(trade_log)

        return BacktestReport(
            mode="portfolio", date_range=input.date_range,
            total_return=round(total_return, 4),
            annual_return=metrics.annual_return,
            sharpe=metrics.sharpe,
            sortino=metrics.sortino, calmar=metrics.calmar,
            omega=metrics.omega, deflated_sharpe=metrics.deflated_sharpe,
            profit_factor=round(pf, 4) if pf != float("inf") else 9999.0,
            expectancy=round(exp, 4),
            max_drawdown=metrics.max_drawdown,
            win_rate=round(win_rate, 4), total_trades=len(trade_log),
            pre_cost_return=round(pre_cost_return, 4),
            post_cost_return=round(total_return, 4),
            cost_impact_pct=round(cost_impact * 100, 4),
            equity_curve=equity_curve, trade_log=trade_log,
            sell_data_missing=sim["sell_data_missing"],
            data_quality={"fill_price": sim["fill_mode"], "entry_date": sim["entry_date"],
                          "exit_date": sim["exit_date"], "entry_skipped": sim["entry_skipped"],
                          "sell_blocked": sim["sell_blocked"]},
            one_line_verdict=f"组合回测: {len(input.codes)}只, 收益{total_return:.2%}, 夏普{metrics.sharpe:.2f}",
        )
