"""Intraday 盘中监控三阶段 — 单元测试"""
import pytest
from strategy_layer.intraday.models import (
    AuctionPoint, PreMarketStock, PreMarketBrief, IntradayAlert,
)


class TestModels:
    def test_auction_point_construction(self):
        ap = AuctionPoint(time="09:25", price=10.5, volume=1000.0, direction="buy")
        assert ap.time == "09:25"
        assert ap.price == 10.5
        assert ap.volume == 1000.0
        assert ap.direction == "buy"

    def test_pre_market_stock_defaults(self):
        s = PreMarketStock(code="000001", name="平安银行")
        assert s.code == "000001"
        assert s.name == "平安银行"
        assert s.auction_trace == []
        assert s.open_price == 0.0
        assert s.change_pct == 0.0
        assert s.volume_ratio == 0.0
        assert s.suggestion == ""

    def test_pre_market_stock_with_auction(self):
        trace = [AuctionPoint("09:20", 10.0, 500, "buy"), AuctionPoint("09:25", 10.2, 800, "buy")]
        s = PreMarketStock(code="000001", name="平安银行", auction_trace=trace, change_pct=1.5, volume_ratio=2.0)
        assert len(s.auction_trace) == 2
        assert s.change_pct == 1.5
        assert s.volume_ratio == 2.0

    def test_pre_market_brief_construction(self):
        stocks = [
            PreMarketStock(code="000001", name="平安银行", change_pct=1.5),
            PreMarketStock(code="000002", name="万科A", change_pct=-0.5),
        ]
        brief = PreMarketBrief(
            date="2026-07-20",
            generated_at="2026-07-20T09:00:00",
            market_summary="测试市场摘要",
            stocks=stocks,
            key_observations=["观察1", "观察2"],
        )
        assert brief.date == "2026-07-20"
        assert len(brief.stocks) == 2
        assert len(brief.key_observations) == 2

    def test_pre_market_brief_defaults(self):
        brief = PreMarketBrief(date="2026-07-20", generated_at="2026-07-20T09:00:00", market_summary="")
        assert brief.stocks == []
        assert brief.key_observations == []

    def test_intraday_alert_construction(self):
        alert = IntradayAlert(
            code="000001",
            name="平安银行",
            trigger_reason="surge",
            current_price=11.5,
            trigger_time="10:30:00",
            urgency="高",
            quick_check="放量上涨",
            quick_check_detail="涨幅5%，量比2.5",
        )
        assert alert.code == "000001"
        assert alert.urgency == "高"
        assert alert.trigger_reason == "surge"

    def test_intraday_alert_defaults(self):
        alert = IntradayAlert(
            code="000001", name="平安银行", trigger_reason="surge",
            current_price=11.5, trigger_time="10:30:00", urgency="中",
        )
        assert alert.quick_check == ""
        assert alert.quick_check_detail == ""


class TestStrategyLayerIntegration:
    def test_strategy_layer_has_intraday_methods(self):
        from strategy_layer import StrategyLayer
        layer = StrategyLayer()
        assert hasattr(layer, "run_pre_market")
        assert hasattr(layer, "run_intraday_cycle")
        assert hasattr(layer, "run_post_market")
        assert hasattr(layer, "intraday_monitor")

    def test_run_pre_market_raises_not_implemented_fake_removed(self):
        """ST-08 拆假货: 假实现已删, 方法显式抛 NotImplementedError (真重建排队, 前置 DL-22)"""
        from strategy_layer import StrategyLayer
        layer = StrategyLayer()
        with pytest.raises(NotImplementedError):
            layer.run_pre_market("2026-07-20")

    def test_run_intraday_cycle_returns_alerts(self):
        from strategy_layer import StrategyLayer
        layer = StrategyLayer()
        alerts = layer.run_intraday_cycle("2026-07-20 10:30:00")
        assert isinstance(alerts, list)

    def test_run_post_market_returns_post_market_result(self):
        from strategy_layer import StrategyLayer, PostMarketResult
        layer = StrategyLayer()
        result = layer.run_post_market("2026-07-20")
        assert isinstance(result, PostMarketResult)
        assert result.date == "2026-07-20"
        assert result.status == "completed"

    def test_run_daily_result_has_no_fake_pre_market_field(self):
        """ST-08 拆假货: DailyRunResult 不再有恒 None 的 pre_market 假字段"""
        from strategy_layer import StrategyLayer
        layer = StrategyLayer()
        result = layer.run_daily("2026-07-20")
        assert not hasattr(result, "pre_market")