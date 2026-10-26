"""E2E: Data → Strategy → Agent → Backtest full chain"""
import pytest
from unittest.mock import MagicMock, patch
from common.database import Database
from data_layer import DataLayer
from data_layer._models import (
    DataSnapshot, Kline, Financial, Valuation, Moneyflow,
    NewsItem, LimitUpDown, MarketState, IndexPoint, ReadinessReport, TableStatus,
)
from strategy_layer import StrategyLayer
from agent_layer import AgentLayer
from agent_layer.core.models import AgentRequest
from backtest_layer import BacktestLayer
from backtest_layer.core.models import CostConfig
from backtest_layer.core.broker import Broker
from backtest_layer.modes.signal import SignalMode


DATE = "2026-07-18"


def _make_snapshot(codes: list[str] | None = None) -> DataSnapshot:
    codes = codes or ["000001", "000002", "600519"]
    klines = [
        Kline(
            code=c, date=DATE, open=10.0, high=11.0, low=9.5, close=10.5,
            volume=1e6, amount=1e7, pre_close=10.0, change_pct=5.0, turnover_rate=2.0,
        )
        for c in codes
    ]
    return DataSnapshot(
        date=DATE,
        market=MarketState(state="normal", indices=[], up_count=2000, down_count=800),
        klines=klines,
        financials=[Financial(code=c, date=DATE, eps=1.0, roe=15.0) for c in codes],
        valuations=[Valuation(code=c, date=DATE, pe_ttm=15.0, pb_mrq=2.0) for c in codes],
        moneyflow=[Moneyflow(code=c, date=DATE) for c in codes],
        news=[],
        limit_up_down=[],
        readiness=ReadinessReport(
            date=DATE, is_ready=True,
            tables={t: TableStatus(table=t, has_data=True, record_count=100) for t in ["klines", "financials"]},
        ),
    )


def _mock_data_layer() -> MagicMock:
    dl = MagicMock(spec=DataLayer)
    dl.snapshot.return_value = _make_snapshot()

    mock_history = MagicMock()
    mock_history.klines = [{"close": 10.5, "high": 11.0, "low": 9.5, "open": 10.0, "volume": 1e6, "amount": 1e7, "turnover_rate": 2.0, "change_pct": 5.0}]
    mock_history.financials = [{"eps": 1.0, "roe": 15.0, "revenue": 1e9, "profit": 1e8, "gross_margin": 0.5, "net_margin": 0.1, "operating_cash_flow": 1e7, "total_assets": 1e10, "total_liabilities": 5e9, "net_assets": 5e9}]
    mock_history.valuations = [{"pe_ttm": 15.0, "pb_mrq": 2.0, "ps_ttm": 3.0, "market_cap": 1e10, "total_shares": 1e9}]
    dl.get_history.return_value = mock_history

    return dl


class TestE2ECoreChain:
    def test_strategy_generates_signals(self):
        mock_dl = _mock_data_layer()
        strategy = StrategyLayer(data_layer=mock_dl)
        result = strategy.run_daily(DATE)

        assert result.date == DATE
        assert isinstance(result.signals, list)
        assert result.regime in ("bull", "bear", "neutral")
        assert result.stock_count == 3

    def test_agent_analyzes_signal(self):
        agent = AgentLayer()
        request = AgentRequest(
            task="stock_analysis",
            input={"code": "000001", "date": DATE, "factor_scores": {"momentum": 0.6}},
        )
        response = agent.analyze(request)

        assert response.code == "000001"
        assert response.verdict in ("推荐", "不推荐", "观望")
        assert response.quality.latency_ms >= 0

    def test_backtest_signal_mode_completes(self):
        broker = Broker(CostConfig(initial_capital=100000))
        mode = SignalMode(broker)

        signals = [
            {"code": "000001", "signal": "buy", "date": DATE},
            {"code": "000001", "signal": "hold", "date": "2026-07-19"},
            {"code": "000001", "signal": "sell", "date": "2026-07-20"},
        ]
        price_data = {"000001": [10.0, 10.5, 10.8]}
        result = mode.run(signals, price_data)

        assert result["mode"] == "signal"
        assert result["trade_count"] >= 1
        assert len(result["equity_curve"]) >= 1

    def test_full_chain_strategy_to_backtest(self):
        mock_dl = _mock_data_layer()
        strategy = StrategyLayer(data_layer=mock_dl)
        daily = strategy.run_daily(DATE)

        signals = []
        for s in daily.signals:
            signals.append({
                "code": s.code,
                "signal": s.action,
                "date": DATE,
            })

        if not signals:
            signals = [
                {"code": "000001", "signal": "buy", "date": DATE},
                {"code": "000001", "signal": "sell", "date": "2026-07-19"},
            ]

        broker = Broker(CostConfig(initial_capital=100000))
        mode = SignalMode(broker)
        price_data = {s["code"]: [10.0, 11.0] for s in signals}
        bt_result = mode.run(signals, price_data)

        assert bt_result["mode"] == "signal"
        assert "final_nav" in bt_result
