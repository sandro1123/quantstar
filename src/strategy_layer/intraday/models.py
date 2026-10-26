from dataclasses import dataclass, field


@dataclass
class MinuteBar:
    """分钟K线 (策略层本地定义，避免跨层导入 data_layer)"""
    code: str
    time: str
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: float = 0.0


@dataclass
class AuctionPoint:
    time: str
    price: float
    volume: float
    direction: str  # "buy" | "sell" | "neutral"


@dataclass
class PreMarketStock:
    code: str
    name: str
    auction_trace: list[AuctionPoint] = field(default_factory=list)
    open_price: float = 0.0
    change_pct: float = 0.0
    volume_ratio: float = 0.0
    suggestion: str = ""


@dataclass
class PreMarketBrief:
    date: str
    generated_at: str
    market_summary: str
    stocks: list[PreMarketStock] = field(default_factory=list)
    key_observations: list[str] = field(default_factory=list)


@dataclass
class IntradayAlert:
    code: str
    name: str
    trigger_reason: str
    current_price: float
    trigger_time: str
    urgency: str  # "高" | "中" | "低"
    quick_check: str = ""
    quick_check_detail: str = ""
