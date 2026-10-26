"""Backtest Layer — 跨层返回类型"""
from dataclasses import dataclass, field


@dataclass
class BacktestMetrics:
    sharpe: float = 0.0
    max_drawdown: float = 0.0
    annual_return: float = 0.0
    sortino: float = 0.0
    calmar: float = 0.0
    omega: float = 0.0
    deflated_sharpe: float = 0.0
    drawdown_duration: int = 0
    recovery_period: int = 0
    profit_factor: float = 0.0
    expectancy: float = 0.0
    rolling_sharpe_20d: list = field(default_factory=list)
    rolling_max_dd_20d: list = field(default_factory=list)
    # BT-06: None = 不可算 (无 trade pnl / 无基准), 不再用 0.0 假值冒充
    sqn: float | None = None
    capture_ratio: float | None = None
    total_return: float = 0.0


@dataclass
class AnalysisReport:
    analyzers: dict = field(default_factory=dict)


@dataclass
class PositionConcentration:
    hhi: float = 0.0
    top5_pct: float = 0.0
    num_positions: int = 0
