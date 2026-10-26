"""backtest_layer/evaluation/strategy_eval.py — 策略评估完整实现

提供策略级别的绩效评估：
- 单策略多周期汇总
- 多策略横向对比
- 月度收益热力图数据
- Alpha/Beta分解
"""

import math
from dataclasses import dataclass, field


@dataclass
class StrategyComparison:
    best_sharpe: dict = field(default_factory=dict)
    best_return: dict = field(default_factory=dict)
    lowest_drawdown: dict = field(default_factory=dict)
    total_strategies: int = 0


@dataclass
class StrategyMetrics:
    name: str = ""
    total_return: float = 0.0
    annual_return: float = 0.0
    sharpe: float = 0.0
    sortino: float = 0.0
    calmar: float = 0.0
    max_drawdown: float = 0.0
    max_dd_duration: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0
    annual_turnover: float = 0.0
    alpha: float = 0.0
    beta: float = 0.0
    total_trades: int = 0
    n_periods: int = 0
    monthly_returns: list[dict] = field(default_factory=list)


class StrategyEvaluator:
    def __init__(self, risk_free_rate: float = 0.03):
        self.risk_free_rate = risk_free_rate
        self.metrics: dict[str, dict] = {}

    def evaluate(self, strategies: dict[str, list[dict]]) -> dict[str, dict]:
        results = {}
        for name, reports in strategies.items():
            sm = self.evaluate_single(name, reports)
            results[name] = {
                "total_return": sm.total_return,
                "annual_return": sm.annual_return,
                "sharpe": sm.sharpe,
                "sortino": sm.sortino,
                "calmar": sm.calmar,
                "max_drawdown": sm.max_drawdown,
                "max_dd_duration": sm.max_dd_duration,
                "win_rate": sm.win_rate,
                "profit_factor": sm.profit_factor,
                "expectancy": sm.expectancy,
                "total_trades": sm.total_trades,
                "n_periods": sm.n_periods,
                "monthly_returns": sm.monthly_returns,
            }
        self.metrics = results
        return results

    def evaluate_single(self, name: str, reports: list[dict]) -> StrategyMetrics:
        if not reports:
            return StrategyMetrics(name=name)
        returns = [r.get("total_return", 0) for r in reports]
        total_return = sum(returns)
        avg_return = total_return / len(returns) if returns else 0
        sharpe = reports[-1].get("sharpe", 0) if reports else 0
        max_dd = max((r.get("max_drawdown", 0) for r in reports), default=0)
        total_trades = sum(r.get("total_trades", 0) for r in reports)
        winning = sum(1 for r in reports for t in r.get("trade_log", []) if t.get("pnl", 0) > 0)
        all_trades_count = sum(len(r.get("trade_log", [])) for r in reports)
        win_rate = winning / all_trades_count if all_trades_count > 0 else 0.0
        gross_profit = sum(
            t.get("pnl", 0) for r in reports for t in r.get("trade_log", []) if t.get("pnl", 0) > 0
        )
        gross_loss = abs(sum(
            t.get("pnl", 0) for r in reports for t in r.get("trade_log", []) if t.get("pnl", 0) < 0
        ))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
        trade_pnls = [t.get("pnl", 0) for r in reports for t in r.get("trade_log", [])]
        expectancy = sum(trade_pnls) / len(trade_pnls) if trade_pnls else 0.0
        days = len(returns)
        annual_return = ((1 + total_return) ** (252 / days) - 1) if days > 0 else 0.0
        downside = [r for r in returns if r < 0]
        down_std = (sum(d ** 2 for d in downside) / len(downside)) ** 0.5 if downside else 1.0
        sortino = (avg_return - self.risk_free_rate / 252) / down_std * math.sqrt(252) if down_std > 0 else 0.0
        calmar = annual_return / max_dd if max_dd > 0 else 0.0
        monthly = self._monthly_returns(returns)
        return StrategyMetrics(
            name=name,
            total_return=round(total_return, 4),
            annual_return=round(annual_return, 4),
            sharpe=round(sharpe, 4),
            sortino=round(sortino, 4),
            calmar=round(calmar, 4),
            max_drawdown=round(max_dd, 4),
            win_rate=round(win_rate, 4),
            profit_factor=round(profit_factor, 4) if profit_factor != float("inf") else 9999.0,
            expectancy=round(expectancy, 4),
            total_trades=total_trades,
            n_periods=len(reports),
            monthly_returns=monthly,
        )

    def _monthly_returns(self, daily_returns: list[float]) -> list[dict]:
        if not daily_returns:
            return []
        months = {}
        for i, r in enumerate(daily_returns):
            month_key = f"month_{i // 21}"
            months.setdefault(month_key, []).append(r)
        result = []
        for key in sorted(months):
            ret = 1.0
            for r in months[key]:
                ret *= (1 + r)
            result.append({"period": key, "return": round(ret - 1, 4)})
        return result

    def compare(self, results: dict[str, StrategyMetrics]) -> StrategyComparison:
        if not results:
            return StrategyComparison()
        best_sharpe = max(results.items(), key=lambda x: x[1].sharpe)
        best_return = max(results.items(), key=lambda x: x[1].total_return)
        lowest_dd = min(results.items(), key=lambda x: x[1].max_drawdown)
        return StrategyComparison(
            best_sharpe={"name": best_sharpe[0], "value": best_sharpe[1].sharpe},
            best_return={"name": best_return[0], "value": best_return[1].total_return},
            lowest_drawdown={"name": lowest_dd[0], "value": lowest_dd[1].max_drawdown},
            total_strategies=len(results),
        )

    def alpha_beta(self, strategy_returns: list[float], benchmark_returns: list[float]) -> tuple[float, float]:
        n = min(len(strategy_returns), len(benchmark_returns))
        if n < 2:
            return 0.0, 0.0
        sr = strategy_returns[:n]
        br = benchmark_returns[:n]
        mean_s = sum(sr) / n
        mean_b = sum(br) / n
        cov = sum((sr[i] - mean_s) * (br[i] - mean_b) for i in range(n)) / n
        var_b = sum((br[i] - mean_b) ** 2 for i in range(n)) / n
        beta = cov / var_b if var_b > 0 else 0.0
        alpha_daily = mean_s - beta * mean_b
        alpha_annual = alpha_daily * 252
        return round(alpha_annual, 4), round(beta, 4)
