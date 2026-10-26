"""结构化数据输出 — 供 Report Layer 消费"""

from dataclasses import dataclass, field


@dataclass
class PortfolioSnapshot:
    active: int = 0
    pending_entry: int = 0
    pending_exit: int = 0
    watch: int = 0
    exited: int = 0
    date: str = ""
    total_invested: float = 0.0
    total_pnl: float = 0.0
    total_pnl_pct: float = 0.0
    total_capital: float = 300000.0
    portfolio_nav: float = 300000.0
    today_pnl: float = 0.0
    available_cash: float = 300000.0
    positions: list = field(default_factory=list)
    pending_entries: list = field(default_factory=list)
    pending_exits: list = field(default_factory=list)


@dataclass
class PositionCard:
    code: str
    name: str
    entry_price: float
    current_price: float
    pnl_pct: float
    holding_days: int
    stop_loss: float
    take_profit: float
    score: float
    risk_flags: list = field(default_factory=list)


@dataclass
class ActionFeed:
    date: str = ""
    actions: list = field(default_factory=list)


@dataclass
class ExitReviewCard:
    code: str
    name: str
    entry_price: float
    exit_price: float
    total_return_pct: float
    holding_days: int
    exit_reason: str
    exit_reason_user: str = ""
    post_exit_t: list = field(default_factory=list)


@dataclass
class StrategySummaryCard:
    date: str = ""
    total_return_pct: float = 0.0
    win_rate: float = 0.0
    sharpe: float = 0.0
    max_drawdown: float = 0.0
    total_trades: int = 0
    active_positions: int = 0


@dataclass
class BestWorstTradeCard:
    best: list = field(default_factory=list)
    worst: list = field(default_factory=list)


@dataclass
class TradeHighlight:
    code: str = ""
    action: str = ""
    price: float = 0.0
    pnl_pct: float = 0.0
    reason: str = ""


@dataclass
class PositionCardV2:
    code: str = ""
    name: str = ""
    entry_price: float = 0.0
    current_price: float = 0.0
    pnl_pct: float = 0.0
    holding_days: int = 0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    score: float = 0.0
    risk_flags: list = field(default_factory=list)
    entry_reason: str = ""
    position_pct: float = 0.0
    reason_chain: str = ""


@dataclass
class IntradayStatsCard:
    time: str = ""
    active_count: int = 0
    total_unrealized_pnl: float = 0.0
    today_pnl: float = 0.0
    best_performer: str = ""
    worst_performer: str = ""
    pending_entries: int = 0
    pending_exits: int = 0


@dataclass
class CashFlowCard:
    code: str = ""
    name: str = ""
    action_type: str = ""
    price: float = 0.0
    quantity: int = 0
    gross_amount: float = 0.0
    fee: float = 0.0
    net_amount: float = 0.0
    timestamp: str = ""


@dataclass
class VolatilityExplanation:
    date: str = ""
    market_change: float = 0.0
    explanation: str = ""
    affected_positions: list = field(default_factory=list)


@dataclass
class RiskExpectationCard:
    """Risk expectation display (G-E28)"""
    worst_case_drawdown: float = 0.0
    var_95: float = 0.0
    max_drawdown: float = 0.0
    current_drawdown: float = 0.0
    risk_level: str = "低"


@dataclass
class DailyPriceRange:
    """Daily price range per position (G-E30)"""
    code: str = ""
    name: str = ""
    date: str = ""
    open_price: float = 0.0
    high_price: float = 0.0
    low_price: float = 0.0
    close_price: float = 0.0
    change_pct: float = 0.0


@dataclass
class TradeClassification:
    short_term: int = 0
    medium_term: int = 0
    long_term: int = 0
    short_win_rate: float = 0.0
    medium_win_rate: float = 0.0
    long_win_rate: float = 0.0
    avg_short_return: float = 0.0
    avg_medium_return: float = 0.0
    avg_long_return: float = 0.0


@dataclass
class TradeFilter:
    total: int = 0
    by_status: dict = field(default_factory=dict)
    by_sector: dict = field(default_factory=dict)
    date_range: tuple = field(default_factory=lambda: ("", ""))


@dataclass
class OrderState:
    code: str = ""
    order_type: str = ""
    state: str = "pending"
    requested_qty: int = 0
    filled_qty: int = 0
    requested_price: float = 0.0
    filled_price: float = 0.0
    created_at: str = ""
    filled_at: str = ""
    reason: str = ""
    state_history: list = field(default_factory=list)


@dataclass
class DrawdownAnalysis:
    max_drawdown_pct: float = 0.0
    max_drawdown_days: int = 0
    max_drawdown_recovery_days: int = 0
    current_drawdown_pct: float = 0.0
    current_drawdown_days: int = 0
    drawdown_periods: list = field(default_factory=list)


@dataclass
class MonthlyHeatmap:
    year: int = 0
    months: list = field(default_factory=list)


@dataclass
class ReturnAttribution:
    total_return_pct: float = 0.0
    stock_selection: float = 0.0
    timing: float = 0.0
    allocation: float = 0.0
    residual: float = 0.0
    num_positive: int = 0
    num_negative: int = 0


@dataclass
class RiskContribution:
    code: str = ""
    name: str = ""
    weight_pct: float = 0.0
    risk_contribution_pct: float = 0.0
    unrealized_pnl_pct: float = 0.0


@dataclass
class RollingMetrics:
    period_20d_return: float = 0.0
    period_60d_return: float = 0.0
    period_20d_win_rate: float = 0.0
    period_60d_win_rate: float = 0.0
    period_20d_sharpe: float = 0.0
    period_60d_sharpe: float = 0.0


@dataclass
class MultiRiskMetrics:
    std_dev_pct: float = 0.0
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    calmar_ratio: float = 0.0
    var_95_pct: float = 0.0
    cvar_95_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    downside_std_pct: float = 0.0


def build_summary_card(stats: dict) -> StrategySummaryCard:
    return StrategySummaryCard(
        date=stats.get("date", ""),
        total_return_pct=stats.get("total_return_pct", 0.0),
        win_rate=stats.get("win_rate", 0.0),
        sharpe=stats.get("sharpe", 0.0),
        max_drawdown=stats.get("max_drawdown", 0.0),
        total_trades=stats.get("total_trades", 0),
        active_positions=stats.get("active_positions", 0),
    )


@dataclass
class TradeRecord:
    code: str
    name: str = ""
    status: str = ""
    entry_price: float = 0.0
    exit_price: float = 0.0
    total_return_pct: float = 0.0
    holding_days: int = 0
    added_date: str = ""
    exit_date: str = ""
    source: str = ""
    entry_reason: str = ""
    exit_reason: str = ""


@dataclass
class ValueCurvePoint:
    date: str
    price: float = 0.0
    pnl_pct: float = 0.0


@dataclass
class PortfolioCurvePoint:
    date: str
    position_value: float = 0.0
    nav: float = 0.0
    pnl_pct: float = 0.0
    benchmark_nav: float = 0.0


@dataclass
class MonthlyStats:
    win_rate: float = 0.0
    avg_return: float = 0.0
    exit_accuracy: float = 0.0
    total_trades: int = 0


@dataclass
class TradeGrouping:
    by_status: dict = field(default_factory=dict)
    by_source: dict = field(default_factory=dict)


@dataclass
class CashFlowRow:
    action: str
    count: int = 0
    total_value: float = 0.0


@dataclass
class AnalyzerResult:
    name: str = ""
    sqn: float = 0.0
    quality: str = ""
    num_trades: int = 0
    avg_return: float = 0.0
    win_rate: float = 0.0
    detail: dict = field(default_factory=dict)


@dataclass
class ExitReview:
    code: str
    name: str = ""
    entry_price: float = 0.0
    exit_price: float = 0.0
    total_return_pct: float = 0.0
    holding_days: int = 0
    exit_reason: str = ""
    post_exit_t: list = field(default_factory=list)
