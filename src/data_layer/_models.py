"""Data Layer 数据模型 — 所有 dataclass"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Kline:
    code: str
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float
    pre_close: float = 0.0
    change_pct: float = 0.0
    turnover_rate: float = 0.0


@dataclass
class Financial:
    code: str
    date: str
    eps: float = 0.0
    roe: float = 0.0
    revenue: float = 0.0
    profit: float = 0.0
    total_assets: float = 0.0
    total_liabilities: float = 0.0
    net_assets: float = 0.0
    operating_cash_flow: float = 0.0
    gross_margin: float = 0.0
    net_margin: float = 0.0


@dataclass
class Valuation:
    code: str
    date: str
    pe_ttm: float | None = None
    pb_mrq: float | None = None
    ps_ttm: float | None = None
    market_cap: float | None = None
    total_shares: float | None = None
    dividend_yield: float | None = None


@dataclass
class Moneyflow:
    code: str
    date: str
    main_net_inflow: float = 0.0
    retail_net_inflow: float = 0.0
    main_inflow: float = 0.0
    main_outflow: float = 0.0


@dataclass
class NewsItem:
    stock_code: str
    title: str
    source: str
    date: str
    sentiment_label: str = "neutral"
    sentiment_score: float = 50.0
    content: str = ""


@dataclass
class LimitUpDown:
    code: str
    name: str
    date: str
    limit_type: str
    price: float = 0.0
    change_pct: float = 0.0
    涨停次数: int = 0


@dataclass
class IndexPoint:
    code: str
    name: str
    current: float = 0.0
    change_pct: float = 0.0


@dataclass
class MarketState:
    state: str
    indices: list[IndexPoint]
    market_temperature: float = 0.0
    up_count: int = 0
    down_count: int = 0


@dataclass
class TableStatus:
    table: str
    has_data: bool = False
    record_count: int = 0
    last_date: str | None = None
    staleness_days: int = -1


@dataclass
class ReadinessReport:
    date: str
    is_ready: bool
    tables: dict[str, TableStatus]


@dataclass
class RealTimeQuote:
    code: str
    name: str
    current: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    volume: float = 0.0
    amount: float = 0.0
    change_pct: float = 0.0
    bid: list = field(default_factory=list)
    ask: list = field(default_factory=list)


@dataclass
class MinuteBar:
    code: str
    time: str
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: float = 0.0


@dataclass
class Anomaly:
    code: str
    name: str
    time: str
    anomaly_type: str = ""
    description: str = ""
    severity: str = "info"


@dataclass
class HotStock:
    code: str
    name: str
    rank: int = 0
    heat: float = 0.0
    reason: str = ""


@dataclass
class DataSnapshot:
    date: str
    market: MarketState
    klines: list[Kline]
    financials: list[Financial]
    valuations: list[Valuation]
    moneyflow: list[Moneyflow]
    news: list[NewsItem]
    limit_up_down: list[LimitUpDown]
    readiness: ReadinessReport


@dataclass
class IntradaySnapshot:
    timestamp: str
    realtime_quotes: list[RealTimeQuote] | None = None
    minute_data: list[MinuteBar] | None = None
    klines_today: list[Kline] | None = None
    limit_up_down: list[LimitUpDown] | None = None
    anomalies: list[Anomaly] | None = None
    hot_stocks: list[HotStock] | None = None
    news: list[NewsItem] | None = None
    capital_flow: Any = None
    market_index: list[IndexPoint] | None = None


@dataclass
class HistoryWindow:
    """历史数据窗口 — 用于因子计算"""
    code: str
    end_date: str
    window: int  # 交易日数
    klines: list = field(default_factory=list)           # list[Kline] 按日期升序
    financials: list = field(default_factory=list)       # list[Financial] 按日期升序
    valuations: list = field(default_factory=list)       # list[Valuation] 按日期升序
    moneyflow: list = field(default_factory=list)        # list[Moneyflow] 按日期升序（可选）


@dataclass
class ConceptData:
    code: str
    date: str
    concept_name: str = ""
    change_pct: float = 0.0


@dataclass
class LhbData:
    code: str
    date: str
    net_buy_amount: float = 0.0
    drama_score: float = 0.0


@dataclass
class AnalystForecast:
    code: str
    date: str
    consensus_score: float = 0.0
    target_price: float = 0.0
    rating: str = ""


@dataclass
class PlateData:
    code: str
    date: str
    plate_name: str = ""
    plate_code: str = ""
    change_pct: float = 0.0
    stock_count: int = 0


@dataclass
class LimitUpData:
    code: str
    date: str
    plate_name: str = ""
    up_limit_times: int = 0
    up_limit_desc: str = ""
    up_limit_reason: str = ""
    fengdan_money: float = 0.0
    turnover_ratio: float = 0.0


@dataclass
class SentimentData:
    date: str
    market_temp: float = 0.0
    limit_up_count: int = 0
    limit_down_count: int = 0
    up_count: int = 0
    down_count: int = 0
    zt_count: int = 0
    dt_count: int = 0


@dataclass
class StockInfo:
    code: str
    name: str = ""
    industry: str = ""
    market: str = "SZ"
    list_date: str = ""
    is_active: bool = True
