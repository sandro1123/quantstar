# ruff: noqa: F401
"""backtest_layer/__init__.py — BacktestLayer 入口"""

from backtest_layer.core.benchmark import Benchmark
from backtest_layer.core.broker import Broker
from backtest_layer.core.data_quality import DataQualityCheck, DataQualityResult
from backtest_layer.core.engine import BacktestEngine
from backtest_layer.core.metrics import Metrics
from backtest_layer.core.models import BacktestInput, BacktestReport, CostConfig
from common.decorators import catch_quantstar_error, with_timeout
from common.errors import ErrorSeverity, QuantStarError
from common.logging import get_logger


class BacktestLayer:
    def __init__(self, data_layer=None, strategy_layer=None, cost: CostConfig | None = None):
        self.data_layer = data_layer
        self.strategy_layer = strategy_layer
        self.cost = cost or CostConfig()
        self.engine = BacktestEngine(self.cost)
        self._run_store = None  # BT-03: 惰性初始化, 未启用时不建表不写库

    def start(self):
        pass

    def health(self):
        from ops_layer.core.models import HealthCheck, LayerHealth
        checks = []

        # 1. Engine readiness
        try:
            engine_ready = self.engine is not None
            checks.append(HealthCheck(name="engine", status="pass" if engine_ready else "fail",
                detail="回测引擎就绪" if engine_ready else "回测引擎未初始化"))
        except Exception as e:
            checks.append(HealthCheck(name="engine", status="fail", detail=f"引擎异常: {e}"))

        # 2. Model availability
        try:
            from strategy_layer.ml.registry import ModelRegistry
            config_obj = getattr(self.data_layer, "config", None) if self.data_layer else None
            registry = ModelRegistry(config=config_obj)
            versions = registry.list_versions() if hasattr(registry, "list_versions") else []
            if versions:
                latest_meta = versions[-1]
                created_at = latest_meta.get("created_at", "unknown")
                model_type = latest_meta.get("model_type", "unknown")
                checks.append(HealthCheck(name="model", status="pass",
                    detail=f"模型: {model_type} ({created_at})"))
            else:
                checks.append(HealthCheck(name="model", status="warn", detail="无已训练的模型"))
        except FileNotFoundError:
            checks.append(HealthCheck(name="model", status="warn", detail="无已训练的模型"))
        except Exception as e:
            checks.append(HealthCheck(name="model", status="warn", detail=f"模型检查异常: {e}"))

        # 3. Data layer connectivity
        dl_ok = self.data_layer is not None
        checks.append(HealthCheck(name="data_layer", status="pass" if dl_ok else "warn",
            detail="数据层已连接" if dl_ok else "数据层未连接"))

        # Overall
        status = "green"
        for c in checks:
            if c.status == "fail":
                status = "red"
                break
            elif c.status == "warn":
                status = "yellow"
        return LayerHealth(layer="backtest", status=status, checks=checks,
            summary=f"Backtest Layer {'正常' if status == 'green' else '需关注'}")

    def shutdown(self, timeout: float = 10.0):
        logger = get_logger("backtest")
        logger.info("Backtest Layer 结果已保存")
        return True

    def _get_run_store(self):
        """BT-03: backtest_runs 落库 — config 开关默认关闭, 需用户 GO 后启用。

        启用: config backtest.persistence.enabled = true (建表属写库操作,
        见 04-回测层审计 BT-03)。未启用或无 db 时返回 None, 行为与修复前一致。
        """
        if self._run_store is not None:
            return self._run_store
        config = getattr(self.data_layer, "config", None) if self.data_layer else None
        enabled = bool(config.get("backtest.persistence.enabled", False)) if config else False
        db = getattr(self.data_layer, "db", None) if self.data_layer else None
        if not enabled or db is None:
            return None
        from backtest_layer.results.persistence import BacktestRunStore
        self._run_store = BacktestRunStore(db)
        return self._run_store

    @catch_quantstar_error(layer_name="backtest")
    def run(self, input: BacktestInput) -> BacktestReport:
        import time
        t0 = time.monotonic()
        report = self.engine.run(input, self.data_layer, self.strategy_layer)
        # BT-03: 出口统一落库 (config 门控, 默认关)。落库失败不影响回测结果返回。
        store = self._get_run_store()
        if store is not None:
            try:
                duration_ms = int((time.monotonic() - t0) * 1000)
                report.run_id = store.save(report, bt_input=input, duration_ms=duration_ms)
            except Exception as e:
                get_logger("backtest").warning(f"backtest_runs 落库失败 (不影响回测结果): {e}")
        return report

    @catch_quantstar_error(layer_name="backtest")
    @with_timeout(timeout_s=30)
    def check_data_quality(self, codes: list[str], date_range: tuple[str, str]) -> DataQualityResult:
        return DataQualityCheck.check(codes, date_range, self.data_layer)

    @catch_quantstar_error(layer_name="backtest")
    def run_portfolio_backtest(self, codes: list[str], weights: list[float] | None = None,
                               date_range: tuple[str, str] | None = None,
                               mode: str = "hold") -> BacktestReport:
        from backtest_layer.core.models import BacktestInput
        if not codes:
            raise QuantStarError(layer="backtest", severity=ErrorSeverity.WARNING,
                                 trace_id="", recoverable=False,
                                 suggestion="codes 不能为空")
        if weights is None:
            weights = [1.0 / len(codes)] * len(codes)
        if len(weights) != len(codes):
            raise QuantStarError(layer="backtest", severity=ErrorSeverity.WARNING,
                                 trace_id="", recoverable=False,
                                 suggestion=f"weights({len(weights)}) != codes({len(codes)})")
        wsum = sum(weights)
        if wsum <= 0:
            raise QuantStarError(layer="backtest", severity=ErrorSeverity.WARNING,
                                 trace_id="", recoverable=False,
                                 suggestion=f"sum(weights)={wsum} <= 0")
        weights = [w / wsum for w in weights]
        if date_range is None:
            from datetime import datetime, timedelta
            end = datetime.now().strftime("%Y-%m-%d")
            start = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
            date_range = (start, end)

        individual_reports = []
        for code in codes:
            input_data = BacktestInput(mode=mode, codes=[code], date_range=date_range)
            report = self.run(input_data)
            individual_reports.append(report)

        # 逐股独立回测 → 组合每日净值曲线（归一化+加权）
        curve_map: dict[str, dict] = {}
        for report, w in zip(individual_reports, weights):
            curve = report.equity_curve
            if not curve:
                continue
            base = curve[0].get("nav", 1.0) or 1.0
            for point in curve:
                date = point.get("date", "")
                if not date:
                    continue
                nav = point.get("nav", base)
                norm = nav / base if base > 0 else 0.0
                curve_map.setdefault(date, 0.0)
                curve_map[date] += w * norm

        total_trades = sum(r.total_trades for r in individual_reports)
        total_wins = sum(int(r.win_rate * r.total_trades) for r in individual_reports)
        win_rate = total_wins / total_trades if total_trades > 0 else 0.0

        if curve_map:
            dates = sorted(curve_map)
            portfolio_curve = [{"date": d, "nav": curve_map[d]} for d in dates]
            nav_series = [curve_map[d] for d in dates]
            from backtest_layer.core.metrics import Metrics
            metrics = Metrics.calc(nav_series)
            total_return = nav_series[-1] - 1.0
            max_drawdown = metrics.max_drawdown
            sharpe = metrics.sharpe
            sortino = metrics.sortino
            calmar = metrics.calmar
            annual_return = metrics.annual_return
        else:
            portfolio_curve = []
            total_return = 0.0
            max_drawdown = 0.0
            sharpe = 0.0
            sortino = 0.0
            calmar = 0.0
            annual_return = 0.0

        from backtest_layer.core.models import BacktestReport
        report = BacktestReport(
            mode="portfolio",
            date_range=date_range,
            total_return=round(total_return, 4),
            annual_return=round(annual_return, 4),
            sharpe=round(sharpe, 4),
            sortino=round(sortino, 4),
            calmar=round(calmar, 4),
            max_drawdown=round(max_drawdown, 4),
            win_rate=round(win_rate, 4),
            total_trades=total_trades,
            profit_factor=round(sum(r.profit_factor * w for r, w in zip(individual_reports, weights)), 4),
            expectancy=round(sum(r.expectancy * w for r, w in zip(individual_reports, weights)), 4),
            equity_curve=portfolio_curve,
            one_line_verdict=f"组合回测: {len(codes)}只股票, 收益{total_return:.2%}",
        )
        # BT-04: 基准对比真接线 (聚合曲线带日期 → attach 对齐真算; 指数缺失降级 None+标注, 绝不回落 0 假值)
        try:
            from backtest_layer.core.benchmark import Benchmark, resolve_benchmark_name
            Benchmark(resolve_benchmark_name(None, self.data_layer)).attach_to_report(report, self.data_layer)
        except Exception as e:
            get_logger("backtest").warning(f"BT-04 基准附加异常(诚实降级): {e}")
            report.benchmark_return = None
            report.excess_return = None
        # BT-08: regime 拆分接线 (聚合曲线带日期 → 按 regime 拆分; 数据缺失诚实降级)
        from backtest_layer.validation.regime_split import attach_regime_breakdown
        attach_regime_breakdown(report, self.strategy_layer)
        return report
