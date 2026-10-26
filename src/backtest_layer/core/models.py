"""backtest_layer/core/models.py — 数据模型"""

from dataclasses import dataclass, field


@dataclass
class CostConfig:
    initial_capital: float = 100000.0
    commission_rate: float = 0.00025
    stamp_tax_rate: float = 0.0005
    slippage_rate: float = 0.001
    min_commission: float = 5.0
    transfer_fee_rate: float = 0.00002


@dataclass
class BacktestInput:
    mode: str
    codes: list[str]
    date_range: tuple[str, str]
    hold_days: int = 5
    cost: CostConfig = field(default_factory=CostConfig)
    # BT-04: 基准默认沪深300 (config backtest.benchmark_index 可配置, "auto" 走 config)
    benchmark: str = "csi300"
    strategy_params: dict = field(default_factory=dict)
    # 成交口径: "next_open" (默认, 信号日次一交易日开盘成交, 无前视) /
    # "same_close" (旧口径: 信号日收盘成交 — 含前视, 仅供复现历史结果)
    fill_price: str = "next_open"


@dataclass
class BacktestReport:
    mode: str
    date_range: tuple[str, str]
    total_return: float
    annual_return: float
    sharpe: float
    max_drawdown: float
    win_rate: float
    total_trades: int
    # BT-04: 基准系字段 = Optional — 指数缺失/数据层无 DL-23 读 API 时降级 None
    # ("宁可没有不可是假的", 绝不回落 0 假值); 可用时真算 (见 core/benchmark.attach_to_report)
    benchmark_return: float | None = None
    excess_return: float | None = None
    sortino: float = 0.0
    calmar: float = 0.0
    omega: float = 0.0
    deflated_sharpe: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0
    pre_cost_return: float = 0.0
    post_cost_return: float = 0.0
    cost_impact_pct: float = 0.0
    equity_curve: list[dict] = field(default_factory=list)
    trade_log: list[dict] = field(default_factory=list)
    strategy_name: str = ""
    # BT-04: 同样 Optional, 无对齐日序时为 None
    information_ratio: float | None = None
    # BT-04: 基准自身风险与相对基准回撤 (超额净值曲线 MDD), 不可算时 None
    benchmark_max_drawdown: float | None = None
    excess_max_drawdown: float | None = None
    # BT-04: 实际使用的指数代码 (如 000300.SH), 供报告展示与追溯
    benchmark_index: str = ""
    regime_breakdown: dict = field(default_factory=dict)
    monthly_returns: list = field(default_factory=list)
    wf_result: dict | None = None
    manifest: dict | None = None
    ic_series: list | None = None
    attribution: dict | None = None
    benchmark_comparison: dict = field(default_factory=dict)
    regime_split: dict = field(default_factory=dict)
    run_id: str = ""
    created_at: str = ""
    one_line_verdict: str = ""
    data_quality_warnings: list[str] = field(default_factory=list)
    # BT-FIX (sm-6d296fb2): 卖出腿缺价票记名 (禁静默) + 数据有效性标记
    # (total_trades=0 时 valid=False, 持久化/消费面据此拒收 — 没考试没分数)
    sell_data_missing: list[str] = field(default_factory=list)
    data_quality: dict = field(default_factory=dict)
