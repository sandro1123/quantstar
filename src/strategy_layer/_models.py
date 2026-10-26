from dataclasses import dataclass

from strategy_layer.selection.selector import SelectionResult
from strategy_layer.signals.generator import TradeSignal


@dataclass
class FactorScores:
    code: str
    date: str
    factors: dict
    composite_score: float


@dataclass
class TradeSignals:
    code: str
    date: str
    signal: str
    strength: float
    composite_score: float


@dataclass
class DailyRunResult:
    date: str
    signals: list[TradeSignal]
    selection: SelectionResult | None
    regime: str
    factor_count: int
    stock_count: int
    # ST-08 (2026-08-18): pre_market 字段已移除 — 原恒为 None 硬编码 (假契约)。
    # 盘中 Phase 1 真重建排队 (前置 DL-22 竞价采集), 届时以独立 API 产出, 不回填假字段。


@dataclass
class PostMarketResult:
    date: str
    status: str
    alerts_count: int
    dashboard: dict | None = None
