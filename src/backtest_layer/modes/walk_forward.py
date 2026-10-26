"""Walk-Forward 模式 — 滚动窗口回测

支持:
- 可配置窗口数 (默认5)
- Anchored expanding 窗口
- 每窗口 IS/OOS split (is_ratio 切分)
- 真实 OOS/IS 效率比 (oos_efficiency)
- 全窗口聚合 Sharpe/MDD/Trades (非仅 window1)
- OOS stability score
"""

import copy
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from backtest_layer.core.benchmark import Benchmark, resolve_benchmark_name
from backtest_layer.core.broker import Broker
from backtest_layer.core.models import BacktestInput, BacktestReport
from backtest_layer.modes.hold import HoldMode


logger = logging.getLogger(__name__)


@dataclass
class WalkForwardConfig:
    n_windows: int = 5
    min_window_days: int = 60
    anchored: bool = False
    is_ratio: float = 0.7


class WalkForwardMode:
    def __init__(self, broker: Broker, data_layer, strategy_layer, config: WalkForwardConfig | None = None):
        self.broker = broker
        self.data_layer = data_layer
        self.strategy_layer = strategy_layer
        self.config = config or WalkForwardConfig()

    def run(self, input: BacktestInput) -> BacktestReport:
        start_date, end_date = input.date_range
        try:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d")
            end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        except ValueError:
            return BacktestReport(
                mode=input.mode, date_range=input.date_range,
                total_return=0.0, annual_return=0.0, sharpe=0.0,
                max_drawdown=0.0, win_rate=0.0, total_trades=0,
                wf_result={"error": "Invalid date format"},
            )

        total_days = (end_dt - start_dt).days
        if total_days < self.config.min_window_days:
            return BacktestReport(
                mode=input.mode, date_range=input.date_range,
                total_return=0.0, annual_return=0.0, sharpe=0.0,
                max_drawdown=0.0, win_rate=0.0, total_trades=0,
                wf_result={"error": f"Date range too short: {total_days} days < {self.config.min_window_days} minimum"},
            )

        windows = self._split_windows(start_dt, end_dt, total_days)

        # 卡#44: concept 覆盖预检 (入口单点, 全 mode 受益)。
        # 缺口根因: get_concept_data 缺日逐股返空表零告警 → signal/factor
        # 模式零填充假值入结果 (违宪法)。消费判据 = strategy_layer 是否
        # 经 build_context 调 get_concept_data (consumes_concept 显式标记,
        # 默认保守 True — 未知策略层按消费处理, 宁可硬失败不可静默假值)。
        # isinstance 真源判别: MagicMock 会伪造任何属性 (getattr 默认防不住,
        # 实证 has attr=True → 预检被激活拆包 MagicMock 炸 ValueError 打死
        # 5 个既有测试), 只信真 DataLayer 实例。
        from data_layer import DataLayer
        cov_api = (self.data_layer.get_concept_coverage
                   if isinstance(self.data_layer, DataLayer) else None)
        consumes = getattr(self.strategy_layer, "consumes_concept", True)
        if cov_api is not None:
            coverage, missing = cov_api(start_date, end_date)
            if coverage < 1.0 and missing:
                if consumes:
                    return BacktestReport(
                        mode="walk_forward", date_range=input.date_range,
                        total_return=0.0, annual_return=0.0, sharpe=0.0,
                        max_drawdown=0.0, win_rate=0.0, total_trades=0,
                        wf_result={"error": (
                            f"concept_daily coverage {coverage:.1%} < 100% "
                            f"in window [{start_date}, {end_date}]; "
                            f"missing dates: {missing}")},
                    )
                # 不消费 concept (如 hold): 只可观测不拦跑 (铁律#12)
                logger.warning(
                    "walk_forward concept coverage %.1f%% < 100%% in window "
                    "[%s, %s]; missing dates: %s (mode does not consume "
                    "concept, continuing)", coverage * 100, start_date,
                    end_date, missing)

        is_reports: list[BacktestReport] = []
        oos_reports: list[BacktestReport] = []
        window_details = []

        for w_start, w_end in windows:
            is_range, oos_range = self._split_is_oos(w_start, w_end)
            is_rep = self._run_hold(is_range, input) if is_range else None
            oos_rep = self._run_hold(oos_range, input) if oos_range else None
            if is_rep is not None:
                is_reports.append(is_rep)
            if oos_rep is not None:
                oos_reports.append(oos_rep)
            window_details.append({
                "start": w_start,
                "end": w_end,
                "is_range": is_range,
                "oos_range": oos_range,
                "is_sharpe": is_rep.sharpe if is_rep else None,
                "oos_sharpe": oos_rep.sharpe if oos_rep else None,
                "oos_return": oos_rep.total_return if oos_rep else None,
            })

        oos_returns = [r.total_return for r in oos_reports]
        avg_oos = sum(oos_returns) / len(oos_returns) if oos_returns else 0.0
        is_returns = [r.total_return for r in is_reports]
        avg_is = sum(is_returns) / len(is_returns) if is_returns else 0.0
        stability = self._stability_score(oos_returns)

        # 全窗口聚合 (非仅 window1)
        agg_oos_sharpe = self._mean([r.sharpe for r in oos_reports])
        agg_is_sharpe = self._mean([r.sharpe for r in is_reports])
        agg_oos_mdd = max((r.max_drawdown for r in oos_reports), default=0.0)
        agg_oos_trades = sum(r.total_trades for r in oos_reports)
        # BT-04: 基准聚合 None 安全 (子窗口指数缺失降级 None, 全缺则 None, 不以 0 均值造假)
        agg_oos_excess = self._mean_opt([r.excess_return for r in oos_reports])
        agg_oos_bench = self._mean_opt([r.benchmark_return for r in oos_reports])
        agg_oos_win_rate = self._mean([r.win_rate for r in oos_reports])
        agg_annual = self._mean([r.annual_return for r in oos_reports])

        # 真实 OOS/IS 效率比: OOS 收益 / IS 收益 (防除零)
        oos_efficiency = (avg_oos / avg_is) if avg_is != 0 else 0.0

        wf_result = {
            "windows": window_details,
            "avg_window_return": round(avg_oos, 4),
            "avg_is_return": round(avg_is, 4),
            "oos_efficiency": round(oos_efficiency, 4),
            "is_sharpe": round(agg_is_sharpe, 4),
            "oos_sharpe": round(agg_oos_sharpe, 4),
            "oos_max_drawdown": round(agg_oos_mdd, 4),
            "oos_total_trades": int(agg_oos_trades),
            "oos_excess_return": agg_oos_excess,
            "stability_score": round(stability, 4),
            "window_count": len(windows),
            "anchored": self.config.anchored,
            "best_window": round(max(oos_returns), 4) if oos_returns else 0.0,
            "worst_window": round(min(oos_returns), 4) if oos_returns else 0.0,
        }

        return BacktestReport(
            mode="walk_forward",
            date_range=input.date_range,
            total_return=round(avg_oos, 4),
            annual_return=round(agg_annual, 4),
            sharpe=round(agg_oos_sharpe, 4),
            max_drawdown=round(agg_oos_mdd, 4),
            win_rate=round(agg_oos_win_rate, 4),
            total_trades=int(agg_oos_trades),
            benchmark_return=agg_oos_bench,
            excess_return=agg_oos_excess,
            wf_result=wf_result,
        )

    def _run_hold(self, date_range: tuple[str, str], input: BacktestInput) -> BacktestReport | None:
        w_start, w_end = date_range
        try:
            ws_dt = datetime.strptime(w_start, "%Y-%m-%d")
            we_dt = datetime.strptime(w_end, "%Y-%m-%d")
        except ValueError:
            return None
        if (we_dt - ws_dt).days < 1:
            return None
        w_input = BacktestInput(
            mode="hold",
            codes=input.codes,
            date_range=(w_start, w_end),
            hold_days=input.hold_days,
            cost=input.cost,
            benchmark=input.benchmark,
            fill_price=getattr(input, "fill_price", "next_open"),
        )
        hold_mode = HoldMode(copy.deepcopy(self.broker), self.data_layer, self.strategy_layer)
        rep = hold_mode.run(w_input)
        # BT-04: 子窗口级基准真算 (指数缺失降级 None, 聚合层 None 安全)
        try:
            Benchmark(resolve_benchmark_name(input.benchmark, self.data_layer)).attach_to_report(rep, self.data_layer)
        except Exception:
            rep.benchmark_return = None
            rep.excess_return = None
            rep.information_ratio = None
            rep.benchmark_max_drawdown = None
            rep.excess_max_drawdown = None
        return rep

    def _split_is_oos(self, w_start: str, w_end: str) -> tuple[tuple[str, str] | None, tuple[str, str] | None]:
        """按 is_ratio 把单个窗口切成 IS / OOS 两段。"""
        ws_dt = datetime.strptime(w_start, "%Y-%m-%d")
        we_dt = datetime.strptime(w_end, "%Y-%m-%d")
        w_days = (we_dt - ws_dt).days
        if w_days < 2:
            return None, (w_start, w_end)
        is_days = max(1, int(w_days * self.config.is_ratio))
        if is_days >= w_days:
            is_days = w_days - 1
        is_end_dt = ws_dt + timedelta(days=is_days)
        oos_start_dt = is_end_dt + timedelta(days=1)
        is_range = (w_start, is_end_dt.strftime("%Y-%m-%d"))
        oos_range = (oos_start_dt.strftime("%Y-%m-%d"), w_end)
        return is_range, oos_range

    @staticmethod
    def _mean(values: list[float]) -> float:
        return sum(values) / len(values) if values else 0.0

    @staticmethod
    def _mean_opt(values):
        """BT-04: None 安全均值 — 全是 None 返回 None (宁可没有不可是假的)。"""
        vals = [v for v in values if v is not None]
        return round(sum(vals) / len(vals), 4) if vals else None

    def _split_windows(self, start_dt: datetime, end_dt: datetime, total_days: int) -> list[tuple[str, str]]:
        n = self.config.n_windows
        if self.config.anchored:
            return self._anchored_windows(start_dt, end_dt, total_days, n)
        return self._rolling_windows(start_dt, end_dt, total_days, n)

    def _rolling_windows(self, start_dt: datetime, end_dt: datetime, total_days: int, n: int) -> list[tuple[str, str]]:
        window_size = total_days // n
        windows = []
        for i in range(n):
            ws = start_dt + timedelta(days=i * window_size)
            we = start_dt + timedelta(days=(i + 1) * window_size - 1) if i < n - 1 else end_dt
            windows.append((ws.strftime("%Y-%m-%d"), we.strftime("%Y-%m-%d")))
        return windows

    def _anchored_windows(self, start_dt: datetime, end_dt: datetime, total_days: int, n: int) -> list[tuple[str, str]]:
        step = total_days // n
        windows = []
        for i in range(1, n + 1):
            we = start_dt + timedelta(days=i * step) if i < n else end_dt
            windows.append((start_dt.strftime("%Y-%m-%d"), we.strftime("%Y-%m-%d")))
        return windows

    def _stability_score(self, returns: list[float]) -> float:
        if len(returns) < 2:
            return 1.0
        positive = sum(1 for r in returns if r > 0)
        pct_positive = positive / len(returns)
        mean_r = sum(returns) / len(returns)
        std_r = (sum((r - mean_r) ** 2 for r in returns) / len(returns)) ** 0.5
        cv = std_r / abs(mean_r) if mean_r != 0 else float("inf")
        consistency_score = pct_positive * 0.6
        cv_score = max(0, 1.0 - cv) * 0.4
        return consistency_score + cv_score
