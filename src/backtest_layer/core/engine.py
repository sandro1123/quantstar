"""backtest_layer/core/engine.py — BacktestEngine（含保护机制）"""

from backtest_layer.core.benchmark import Benchmark, resolve_benchmark_name
from backtest_layer.core.broker import Broker
from backtest_layer.core.exposure import factor_exposure
from backtest_layer.core.metrics import Metrics
from backtest_layer.core.models import BacktestInput, BacktestReport, CostConfig
from backtest_layer.modes.hold import HoldMode
from backtest_layer.modes.portfolio import PortfolioMode
from common.errors import QuantStarError
from common.logging import get_logger
from common.utils import generate_trace_id


class BacktestProtection:
    """回测保护机制 — 冷却/最大日交易/最大回撤"""
    def __init__(self, max_daily_trades: int = 3, cooldown_days: int = 1,
                 max_drawdown_pct: float = 0.25):
        self.max_daily_trades = max_daily_trades
        self.cooldown_days = cooldown_days
        self.max_drawdown_pct = max_drawdown_pct
        self._daily_trades: dict[str, int] = {}
        self._last_trade_date: str | None = None
        self._peak_nav = 0.0
        self._stopped = False

    def check(self, date: str, current_nav: float, initial_capital: float) -> str | None:
        if self._stopped:
            return "protection_stopped"
        daily_count = self._daily_trades.get(date, 0)
        if daily_count >= self.max_daily_trades:
            return f"max_daily_trades({self.max_daily_trades})"
        self._peak_nav = max(self._peak_nav, current_nav)
        dd = (self._peak_nav - current_nav) / self._peak_nav if self._peak_nav > 0 else 0
        if dd > self.max_drawdown_pct:
            self._stopped = True
            return f"max_drawdown({dd:.1%})"
        return None

    def record_trade(self, date: str):
        self._daily_trades[date] = self._daily_trades.get(date, 0) + 1
        self._last_trade_date = date


class BacktestEngine:
    def __init__(self, cost: CostConfig | None = None, protection: BacktestProtection | None = None):
        self.cost = cost or CostConfig()
        self.protection = protection or BacktestProtection()
        self._trace_id = generate_trace_id()

    def run(self, input: BacktestInput, data_layer=None, strategy_layer=None) -> BacktestReport:
        logger = get_logger("backtest_layer.engine", self._trace_id)
        logger.info(f"BacktestEngine.run: mode={input.mode}, codes={len(input.codes)}, range={input.date_range}")

        # BT-07: 数据质量前置检查 — hold/portfolio/walk_forward 依赖 data_layer。
        # 严重缺口 (完整率<0.5) 拒绝回测; 一般缺口仅标注 data_quality_warnings。
        # 检查自身异常时跳过 (不阻塞回测), 宁可没有不可是假的。
        quality_warnings: list[str] = []
        if data_layer is not None and input.mode in ("hold", "portfolio", "walk_forward"):
            from backtest_layer.core.data_quality import DataQualityCheck
            try:
                dq = DataQualityCheck.check(input.codes, input.date_range, data_layer)
                quality_warnings = list(dq.warnings)
                if not dq.passed:
                    raise QuantStarError(
                        layer="backtest", severity="error", trace_id=self._trace_id,
                        recoverable=True,
                        detail=f"数据质量不合格, 拒绝回测: {'; '.join(dq.warnings[:3])}")
            except QuantStarError:
                raise
            except Exception as e:
                logger.warning(f"数据质量检查异常 (跳过, 不阻塞回测): {e}")

        broker = Broker(input.cost)
        if input.mode == "hold":
            mode = HoldMode(broker, data_layer, strategy_layer)
            report = mode.run(input)
        elif input.mode == "portfolio":
            mode = PortfolioMode(broker, data_layer, strategy_layer)
            report = mode.run(input)
        elif input.mode == "walk_forward":
            from backtest_layer.modes.walk_forward import WalkForwardMode
            mode = WalkForwardMode(broker, data_layer, strategy_layer)
            report = mode.run(input)
        elif input.mode == "signal":
            from backtest_layer.modes.signal import SignalMode
            mode = SignalMode(broker)
            result = mode.run(
                input.strategy_params.get("signals", []),
                input.strategy_params.get("price_data", {}),
            )
            equity_curve = result.get("equity_curve", [])
            nav_series = [equity_curve[0]] if isinstance(equity_curve[0], float) else [e.get("nav", e) for e in equity_curve] if equity_curve else []
            if not nav_series:
                nav_series = [self.cost.initial_capital, result["final_nav"]]
            # BT-06: SQN 用每笔交易 pnl 真算 (无 trade_log 时 Metrics.calc 内标 None)
            trade_pnls = [t["pnl"] for t in result.get("trade_log", []) if "pnl" in t]
            metrics = Metrics.calc(nav_series, trade_pnls=trade_pnls or None)
            total_return = (result["final_nav"] / self.cost.initial_capital - 1) if self.cost.initial_capital > 0 else 0.0
            winning = result.get("winning_trades", 0)
            total = result.get("trade_count", 0)
            win_rate = winning / total if total > 0 else 0.0
            report = BacktestReport(
                mode="signal",
                date_range=input.date_range,
                strategy_name="signal_mode",
                total_return=round(total_return, 4),
                annual_return=metrics.annual_return,
                sharpe=metrics.sharpe,
                sortino=metrics.sortino,
                calmar=metrics.calmar,
                omega=metrics.omega,
                deflated_sharpe=metrics.deflated_sharpe,
                max_drawdown=metrics.max_drawdown,
                win_rate=round(win_rate, 4),
                total_trades=total,
                equity_curve=equity_curve,
                trade_log=result.get("trade_log", []),
                run_id=self._trace_id,
                created_at="",
                attribution=factor_exposure(
                    [(nav_series[i] / nav_series[i-1] - 1) for i in range(1, len(nav_series))],
                ),
            )
        else:
            raise QuantStarError(layer="backtest", severity="error",
                                 trace_id=self._trace_id, recoverable=False,
                                 detail=f"不支持的回测模式: {input.mode}")

        # BT-07: 缺口/涨跌停标注进报告 (不改变回测结果, 只附加元信息)
        # BT-04: 基准对比真接线 (统一附加点, 覆盖 hold/portfolio/signal/walk_forward)
        # 指数缺失诚实降级 None+标注, 绝不回落 0 假值; attach 自身异常亦降级不阻塞
        try:
            Benchmark(resolve_benchmark_name(input.benchmark, data_layer)).attach_to_report(report, data_layer)
        except Exception as e:
            logger.warning(f"BT-04 基准附加异常(诚实降级): {e}")
            report.benchmark_return = None
            report.excess_return = None
            report.information_ratio = None
            report.benchmark_max_drawdown = None
            report.excess_max_drawdown = None
            report.benchmark_comparison = {"available": False, "reason": f"基准附加异常: {e}"}
            warn = f"基准不可用: 附加异常 {e}"
            if warn not in report.data_quality_warnings:
                report.data_quality_warnings.append(warn)

        # BT-08: regime 拆分接线 (统一附加点, 覆盖 hold/portfolio/signal/walk_forward)
        # regime 数据缺失时诚实降级 available=False+覆盖率标注, 绝不静默恒空
        from backtest_layer.validation.regime_split import attach_regime_breakdown
        attach_regime_breakdown(report, strategy_layer)

        if quality_warnings:
            # BT-04: 改为 extend, 避免覆盖基准降级警告
            report.data_quality_warnings.extend(quality_warnings)
        return report
