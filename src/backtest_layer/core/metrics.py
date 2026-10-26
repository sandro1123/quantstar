"""backtest_layer/core/metrics.py — 指标计算"""

import math

from backtest_layer._models import BacktestMetrics, PositionConcentration

_EULER_GAMMA = 0.5772156649


def _norm_cdf(x: float) -> float:
    """标准正态分布 CDF (使用 erf 近似)"""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """标准正态分布 PPF (分位数函数) — 使用理性近似"""
    if p <= 0.0:
        return -float("inf")
    if p >= 1.0:
        return float("inf")
    # 使用 Abramowitz and Stegun 近似
    t = math.sqrt(-2.0 * math.log(1.0 - p if p > 0.5 else p))
    c = [2.515517, 0.802853, 0.010328]
    d = [1.432788, 0.189269, 0.001308]
    x = t - (c[0] + c[1] * t + c[2] * t * t) / (1.0 + d[0] * t + d[1] * t * t + d[2] * t * t * t)
    return x if p > 0.5 else -x


class Metrics:
    @staticmethod
    def calc(equity_curve: list[float], risk_free_rate: float = 0.03,
             geometric: bool = False, trading_days: int = 252,
             trade_pnls: list[float] | None = None,
             benchmark_returns: list[float] | None = None,
             num_trials: int = 1) -> BacktestMetrics:
        """BT-06 (2026-08-18) 假指标修复:
        - geometric 参数真生效 (原被下方算术式二次覆盖, 参数恒无效);
        - SQN 用每笔交易 pnl 计算 (原用日收益序列, 对象错误); 无 trade_pnls
          时返回 None 显式"不可算", 不再拿日收益冒充;
        - capture_ratio 需基准收益序列 (原自指: 用自己收益拆 up/down, 无基准参与,
          数字无意义); 无 benchmark_returns 时返回 None, 待 BT-04/DL-23 基准就绪后真算;
        - deflated_sharpe 的 num_trials 由调用方从调参记录真实传入 (原恒 1,
          多重测试校正从未生效); 无调参记录时保持 1 并知悉校正不生效。
        """
        if len(equity_curve) < 2:
            return BacktestMetrics()
        returns = [
            equity_curve[i] / equity_curve[i - 1] - 1
            for i in range(1, len(equity_curve))
        ]
        avg_return = sum(returns) / len(returns) if returns else 0
        variance = (
            sum((r - avg_return) ** 2 for r in returns) / len(returns) if returns else 0
        )
        std = math.sqrt(variance)

        # Geometric (compound) vs arithmetic return — BT-06: 参数真生效, 不再被二次覆盖
        if geometric and len(returns) > 0:
            total_return = math.prod(1 + r for r in returns) - 1
        else:
            total_return = (equity_curve[-1] / equity_curve[0] - 1) if equity_curve[0] > 0 else 0.0

        annual_return = (1 + total_return) ** (trading_days / len(equity_curve)) - 1 if len(equity_curve) > 0 else 0.0

        sharpe = (
            (avg_return - risk_free_rate / trading_days) / std * math.sqrt(trading_days)
            if std > 0 else 0.0
        )

        # Sortino ratio: uses downside deviation
        downside_returns = [r for r in returns if r < 0]
        downside_var = (
            sum((r - 0) ** 2 for r in downside_returns) / len(downside_returns)
            if downside_returns else 0
        )
        downside_std = math.sqrt(downside_var)
        sortino = (
            (avg_return - risk_free_rate / 252) / downside_std * math.sqrt(252)
            if downside_std > 0
            else 0.0
        )

        peak = equity_curve[0]
        max_dd = 0.0
        max_dd_start = 0
        max_dd_end = 0
        current_dd_start = 0
        for i, v in enumerate(equity_curve):
            if v > peak:
                peak = v
                current_dd_start = i
            dd = (peak - v) / peak
            if dd > max_dd:
                max_dd = dd
                max_dd_start = current_dd_start
                max_dd_end = i

        # BT-06: 原此处有算术式二次覆盖 total_return/annual_return, 导致 geometric
        # 参数恒无效 — 已删除, 上方按 geometric 参数计算的值为最终值。

        # Calmar ratio: annual return / max drawdown
        calmar = annual_return / max_dd if max_dd > 0 else 0.0

        # Omega ratio: probability-weighted gain/loss
        threshold = risk_free_rate / trading_days
        gains = sum(r - threshold for r in returns if r > threshold)
        losses = sum(threshold - r for r in returns if r <= threshold)
        omega = gains / losses if losses > 0 else float("inf")

        # Drawdown duration and recovery period
        drawdown_duration = max_dd_end - max_dd_start if max_dd_end > max_dd_start else 0
        recovery_period = max(0, len(equity_curve) - 1 - max_dd_end)

        # SQN (System Quality Number) — BT-06: 用每笔交易 pnl 计算 (原用日收益序列,
        # 对象错误; 原处还有一行孤儿表达式 winning_trades/total_trades 已删)。
        # 无 trade_pnls 时显式 None "不可算" — 宁可没有, 不可是假的。
        if trade_pnls:
            sqn_avg = sum(trade_pnls) / len(trade_pnls)
            sqn_std = (sum((p - sqn_avg) ** 2 for p in trade_pnls) / len(trade_pnls)) ** 0.5
            sqn = sqn_avg / sqn_std * math.sqrt(len(trade_pnls)) if sqn_std > 0 else 0.0
        else:
            sqn = None

        # Capture ratio — BT-06: 需真实基准收益序列 (原自指: 用自己收益拆 up/down
        # 算捕获比, 无基准参与, 数字无意义)。BT-04/DL-23 基准就绪前返回 None 不造假。
        if benchmark_returns:
            n = min(len(returns), len(benchmark_returns))
            up_idx = [i for i in range(n) if benchmark_returns[i] > 0]
            down_idx = [i for i in range(n) if benchmark_returns[i] < 0]
            up_capture = (sum(returns[i] for i in up_idx) / sum(benchmark_returns[i] for i in up_idx)) if up_idx else None
            down_capture = (sum(returns[i] for i in down_idx) / sum(benchmark_returns[i] for i in down_idx)) if down_idx else None
            capture_ratio = round(up_capture / down_capture, 4) if (up_capture is not None and down_capture) else None
        else:
            capture_ratio = None

        # Rolling metrics (20-day window)
        window = min(20, len(returns))
        rolling_sharpe_20d = []
        rolling_max_dd_20d = []
        for i in range(len(equity_curve) - window):
            segment = equity_curve[i:i + window + 1]
            seg_returns = [segment[j] / segment[j - 1] - 1 for j in range(1, len(segment))]
            seg_avg = sum(seg_returns) / len(seg_returns) if seg_returns else 0
            seg_var = sum((r - seg_avg) ** 2 for r in seg_returns) / len(seg_returns) if seg_returns else 0
            seg_std = math.sqrt(seg_var)
            rs = (seg_avg - risk_free_rate / trading_days) / seg_std * math.sqrt(trading_days) if seg_std > 0 else 0.0
            rolling_sharpe_20d.append(round(rs, 4))
            seg_peak = segment[0]
            seg_max_dd = 0.0
            for v in segment:
                seg_peak = max(seg_peak, v)
                dd = (seg_peak - v) / seg_peak
                seg_max_dd = max(seg_max_dd, dd)
            rolling_max_dd_20d.append(round(seg_max_dd, 4))

        return BacktestMetrics(
            sharpe=round(sharpe, 4),
            # BT-06: num_trials 由调用方从调参记录真实传入 (原恒 1 → 校正从未生效)
            deflated_sharpe=round(Metrics.deflated_sharpe(sharpe, len(returns), num_trials=num_trials), 4),
            max_drawdown=round(max_dd, 4),
            annual_return=round(annual_return, 4),
            sortino=round(sortino, 4) if sortino != 0 else 0.0,
            calmar=round(calmar, 4) if calmar != 0 else 0.0,
            omega=round(omega, 4) if omega != float("inf") else 9999.0,
            drawdown_duration=drawdown_duration,
            recovery_period=recovery_period,
            rolling_sharpe_20d=rolling_sharpe_20d,
            rolling_max_dd_20d=rolling_max_dd_20d,
            sqn=round(sqn, 4) if sqn is not None else None,
            capture_ratio=capture_ratio,
            total_return=round(total_return, 4),
        )

    @staticmethod
    def profit_factor(trade_log: list[dict]) -> float:
        gross_profit = sum(t.get("pnl", 0) for t in trade_log if t.get("pnl", 0) > 0)
        gross_loss = abs(sum(t.get("pnl", 0) for t in trade_log if t.get("pnl", 0) < 0))
        return gross_profit / gross_loss if gross_loss > 0 else float("inf")

    @staticmethod
    def expectancy(trade_log: list[dict]) -> float:
        if not trade_log:
            return 0.0
        total_pnl = sum(t.get("pnl", 0) for t in trade_log)
        return total_pnl / len(trade_log)

    @staticmethod
    def position_concentration(positions: dict[str, dict], prices: dict[str, float],
                                total_capital: float) -> PositionConcentration:
        if not positions or total_capital <= 0:
            return PositionConcentration()
        weights = {}
        for code, pos in positions.items():
            value = prices.get(code, 0) * pos["shares"]
            weights[code] = value / total_capital
        hhi = sum(w ** 2 for w in weights.values())
        sorted_w = sorted(weights.values(), reverse=True)
        top5_pct = sum(sorted_w[:5])
        return PositionConcentration(
            hhi=round(hhi, 4),
            top5_pct=round(top5_pct, 4),
            num_positions=len(positions),
        )

    @staticmethod
    def turnover_rate(trades: list[dict], total_capital: float) -> float:
        if not trades or total_capital <= 0:
            return 0.0
        total_volume = sum(abs(t.get("pnl", 0) or 0) for t in trades)
        return round(total_volume / total_capital, 4)

    @staticmethod
    def deflated_sharpe(sharpe: float, num_observations: int, num_trials: int = 1) -> float:
        """Deflated Sharpe Ratio — 校正多重测试偏差"""
        if num_observations < 2 or sharpe == 0.0:
            return 0.0
        expected_max_sr = math.sqrt(2 * math.log(num_trials)) if num_trials > 1 else 0.0
        adj_sharpe = sharpe * math.sqrt(num_observations - 1)
        numerator = adj_sharpe - expected_max_sr
        inner = 1.0 - _EULER_GAMMA * sharpe + (num_observations - 1) * 0.5 / num_observations
        if inner <= 0:
            return 1.0 if numerator > 0 else 0.0
        denominator = math.sqrt(inner)
        dsr = _norm_cdf(numerator / denominator)
        return round(dsr, 4)
