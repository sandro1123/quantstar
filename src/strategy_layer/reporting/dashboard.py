"""绩效仪表盘 - 标准化绩效报告"""
from dataclasses import dataclass, field


@dataclass
class PerformanceMetrics:
    annualized_return: float = 0.0
    sharpe: float = 0.0
    sortino: float = 0.0
    calmar: float = 0.0
    max_drawdown: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    total_trades: int = 0
    benchmark_return: float = 0.0
    excess_return: float = 0.0


@dataclass
class RollingMetrics:
    """滚动 6 个月指标"""
    date: str
    sharpe_6m: float = 0.0
    return_6m: float = 0.0
    max_dd_6m: float = 0.0


@dataclass
class PerformanceDashboard:
    date: str
    metrics: PerformanceMetrics = field(default_factory=PerformanceMetrics)
    rolling: list[RollingMetrics] = field(default_factory=list)
    factor_attribution: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class PerformanceDashboardBuilder:
    """构建绩效仪表盘"""

    def __init__(self, db=None):
        self.db = db

    def build(self, date: str, returns: list[float] | None = None,
              win_rates: list[float] | None = None,
              benchmark_returns: list[float] | None = None) -> PerformanceDashboard:
        """Build performance dashboard"""
        import numpy as np

        dashboard = PerformanceDashboard(date=date)

        if not returns or len(returns) < 5:
            dashboard.warnings.append("Insufficient data for metrics")
            return dashboard

        arr = np.array(returns)
        win_arr = np.array(win_rates) if win_rates else np.array([1.0 if r > 0 else 0.0 for r in returns])

        # Annualized return
        ann_return = float(np.mean(arr) * 252)

        # Sharpe
        sharpe = float(np.mean(arr) / np.std(arr) * np.sqrt(252)) if np.std(arr) > 1e-10 else 0.0

        # Sortino (downside deviation)
        downside = arr[arr < 0]
        sortino = float(np.mean(arr) / np.std(downside) * np.sqrt(252)) if len(downside) > 0 and np.std(downside) > 1e-10 else 0.0

        # Max drawdown
        cummax = np.maximum.accumulate(arr)
        drawdown = (cummax - arr) / cummax
        max_dd = float(np.max(drawdown)) if len(drawdown) > 0 else 0.0

        # Calmar
        calmar = ann_return / max_dd if max_dd > 0 else 0.0

        # Win rate
        win_rate = float(np.mean(win_arr > 0)) if len(win_arr) > 0 else 0.0

        # Profit factor
        gains = arr[arr > 0].sum()
        losses = abs(arr[arr < 0].sum())
        profit_factor = float(gains / losses) if losses > 0 else float('inf')

        # Benchmark
        bench_arr = np.array(benchmark_returns) if benchmark_returns else np.zeros_like(arr)
        bench_return = float(np.mean(bench_arr) * 252)
        excess = ann_return - bench_return

        dashboard.metrics = PerformanceMetrics(
            annualized_return=round(ann_return, 4),
            sharpe=round(sharpe, 4),
            sortino=round(sortino, 4),
            calmar=round(calmar, 4),
            max_drawdown=round(max_dd, 4),
            win_rate=round(win_rate, 4),
            profit_factor=round(profit_factor, 4),
            total_trades=len(returns),
            benchmark_return=round(bench_return, 4),
            excess_return=round(excess, 4),
        )

        # Rolling 6-month metrics
        if len(returns) >= 126:  # ~6 months
            step = 21  # ~1 month step
            for i in range(0, len(returns) - 125, step):
                window = arr[i:i+126]
                w_sharpe = float(np.mean(window) / np.std(window) * np.sqrt(252)) if np.std(window) > 0 else 0.0
                w_return = float(np.mean(window) * 252)
                w_cummax = np.maximum.accumulate(window)
                w_dd = (w_cummax - window) / w_cummax
                w_max_dd = float(np.max(w_dd)) if len(w_dd) > 0 else 0.0
                dashboard.rolling.append(RollingMetrics(
                    date=f"rolling_{i//21}",
                    sharpe_6m=round(w_sharpe, 4),
                    return_6m=round(w_return, 4),
                    max_dd_6m=round(w_max_dd, 4),
                ))

        return dashboard

    def monthly_returns(self, returns: list[float], dates: list[str]) -> dict[str, dict[str, float]]:
        """月度收益热力图数据

        Returns:
            {year: {month: return}}
        """
        result = {}
        for ret, dt in zip(returns, dates):
            year = dt[:4]
            month = f"{int(dt[5:7]):02d}"
            if year not in result:
                result[year] = {}
            result[year][month] = result[year].get(month, 0) + float(ret)
        return result

    def rolling_beta_alpha(self, returns: list[float], benchmark: list[float],
                           window: int = 63) -> list[dict]:
        """滚动 Beta 和 Alpha

        Args:
            returns: 策略日收益率
            benchmark: 基准日收益率
            window: 滚动窗口（默认63个交易日≈3个月）
        Returns:
            [{date, beta, alpha}]
        """
        import numpy as np
        result = []
        n = min(len(returns), len(benchmark))
        for i in range(window, n, 21):
            r = np.array(returns[i-window:i])
            b = np.array(benchmark[i-window:i])
            if np.std(b) > 1e-10:
                beta = np.cov(r, b)[0, 1] / np.var(b)
                alpha = np.mean(r) - beta * np.mean(b)
                result.append({
                    "date": f"rolling_{i//21}",
                    "beta": round(float(beta), 4),
                    "alpha": round(float(alpha * 252), 6),
                })
        return result

    def benchmark_comparison(self, returns: list[float],
                              benchmark: list[float]) -> dict:
        """基准对比分析

        Returns:
            {excess_return, tracking_error, information_ratio, beta, alpha, r_squared}
        """
        import numpy as np
        from scipy import stats
        n = min(len(returns), len(benchmark))
        r = np.array(returns[:n])
        b = np.array(benchmark[:n])
        excess = r - b
        te = float(np.std(excess) * np.sqrt(252)) if np.std(excess) > 0 else 0.0
        ir = float(np.mean(excess) / np.std(excess) * np.sqrt(252)) if np.std(excess) > 1e-10 else 0.0
        if np.std(b) > 1e-10:
            beta = float(np.cov(r, b)[0, 1] / np.var(b))
            alpha = float(np.mean(r) - beta * np.mean(b)) * 252
            corr = stats.pearsonr(r, b)[0] if len(r) > 2 else 0.0
            r_sq = float(corr ** 2)
        else:
            beta = alpha = r_sq = 0.0
        return {
            "excess_return": round(float(np.mean(excess) * 252), 4),
            "tracking_error": round(te, 4),
            "information_ratio": round(ir, 4),
            "beta": round(beta, 4),
            "alpha": round(alpha, 6),
            "r_squared": round(r_sq, 4),
        }
