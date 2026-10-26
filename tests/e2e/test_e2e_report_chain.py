"""E2E: Strategy → Execution → Report → Delivery chain"""
import pytest
from unittest.mock import MagicMock, patch
from common.database import Database
from data_layer import DataLayer
from data_layer._models import (
    DataSnapshot, Kline, Financial, Valuation, Moneyflow,
    NewsItem, LimitUpDown, MarketState, IndexPoint, ReadinessReport, TableStatus,
)
from strategy_layer import StrategyLayer
from execution_layer import ExecutionLayer
from report_layer import ReportLayer
from delivery_layer import DeliveryLayer
from delivery_layer.message import Message


DATE = "2026-07-18"


def _make_snapshot() -> DataSnapshot:
    codes = ["000001", "000002", "600519"]
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
        financials=[Financial(code=c, date=DATE) for c in codes],
        valuations=[Valuation(code=c, date=DATE, pe_ttm=15.0, pb_mrq=2.0) for c in codes],
        moneyflow=[Moneyflow(code=c, date=DATE) for c in codes],
        news=[],
        limit_up_down=[],
        readiness=ReadinessReport(
            date=DATE, is_ready=True,
            tables={t: TableStatus(table=t, has_data=True, record_count=100) for t in ["klines"]},
        ),
    )


def _mock_data_layer() -> MagicMock:
    from unittest.mock import MagicMock
    dl = MagicMock(spec=DataLayer)
    dl.snapshot.return_value = _make_snapshot()
    mock_history = MagicMock()
    mock_history.klines = [{"close": 10.5, "high": 11.0, "low": 9.5, "open": 10.0, "volume": 1e6, "amount": 1e7, "turnover_rate": 2.0, "change_pct": 5.0}]
    mock_history.financials = [{"eps": 1.0, "roe": 15.0, "revenue": 1e9, "profit": 1e8, "gross_margin": 0.5, "net_margin": 0.1, "operating_cash_flow": 1e7, "total_assets": 1e10, "total_liabilities": 5e9, "net_assets": 5e9}]
    mock_history.valuations = [{"pe_ttm": 15.0, "pb_mrq": 2.0, "ps_ttm": 3.0, "market_cap": 1e10, "total_shares": 1e9}]
    dl.get_history.return_value = mock_history
    return dl


class TestE2EReportChain:
    def test_strategy_produces_signals(self):
        mock_dl = _mock_data_layer()
        strategy = StrategyLayer(data_layer=mock_dl)
        result = strategy.run_daily(DATE)

        assert result.date == DATE
        assert isinstance(result.signals, list)

    def test_execution_layer_portfolio_snapshot(self):
        db = Database(":memory:")
        db.init_db()
        execution = ExecutionLayer(db=db)
        execution.start()

        snap = execution.get_portfolio_snapshot()
        assert hasattr(snap, "active")
        assert hasattr(snap, "watch")
        assert isinstance(snap.active, int)

    def test_evening_report_generation(self):
        mock_dl = _mock_data_layer()
        db = Database(":memory:")
        db.init_db()
        execution = ExecutionLayer(db=db)
        execution.start()

        report_layer = ReportLayer(
            data_layer=mock_dl,
            execution_layer=execution,
        )
        msg = report_layer.build_evening_report(DATE)

        assert msg is not None
        assert isinstance(msg, Message)
        assert msg.content_type == "evening_report"
        assert msg.title != ""

    def test_delivery_layer_send(self):
        message = Message(
            content={"sections": [{"title": "测试", "body": "内容"}], "date": DATE},
            content_type="evening_report",
            title="E2E 测试报告",
            force_channel="feishu_group",
        )
        delivery = DeliveryLayer()

        with patch.object(delivery.executor, "send") as mock_send:
            from delivery_layer.channels.base import DeliveryResult
            mock_send.return_value = [DeliveryResult(
                message_id="test123", channel="feishu_group", status="success", error_msg=""
            )]
            results = delivery.send(message, report_mode="debug")

            assert len(results) >= 1

    def test_full_chain_strategy_to_delivery(self):
        mock_dl = _mock_data_layer()
        strategy = StrategyLayer(data_layer=mock_dl)
        daily = strategy.run_daily(DATE)

        db = Database(":memory:")
        db.init_db()
        execution = ExecutionLayer(db=db)
        execution.start()

        report_layer = ReportLayer(
            data_layer=mock_dl,
            execution_layer=execution,
            strategy_layer=strategy,
        )
        msg = report_layer.build_evening_report(DATE)
        assert msg is not None

        delivery = DeliveryLayer()
        with patch.object(delivery.executor, "send") as mock_send:
            from delivery_layer.channels.base import DeliveryResult
            mock_send.return_value = [DeliveryResult(
                message_id="test456", channel="feishu_group", status="success", error_msg=""
            )]
            results = delivery.send(msg, report_mode="debug")
            assert len(results) >= 1
