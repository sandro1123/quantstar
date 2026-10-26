"""IntradayStrategyMonitor 单测 — ST-02 后行为 (改调 get_realtime_prices, 删 volume_ratio=2.0 假数据)

原测试断言的是坏行为 (snapshot_intraday(code,date) 签名错位恒抛异常被吞 + volume>0→2.0 硬编码),
按"拆假货保留测试资产价值"原则重写为断言新行为。
"""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from strategy_layer.intraday.monitor import IntradayStrategyMonitor


@pytest.fixture
def monitor():
    return IntradayStrategyMonitor()


def make_dl(price: float | None = 10.65, prev_close: float | None = 10.0):
    """mock data_layer: get_realtime_prices + get_history (前收)"""
    dl = MagicMock()
    dl.get_realtime_prices.return_value = {"000001.SZ": price} if price else {}
    klines = []
    if prev_close:
        klines = [
            SimpleNamespace(date="2026-07-17", close=prev_close),
            SimpleNamespace(date="2026-07-20", close=99.0),  # 当日行, 应被 date< 过滤
        ]
    dl.get_history.return_value = SimpleNamespace(klines=klines)
    return dl


class TestCheckStock:
    def test_no_data_layer(self, monitor):
        assert monitor._check_stock("000001.SZ", "2026-07-20") is None

    def test_no_realtime_price(self, monitor):
        dl = MagicMock()
        dl.get_realtime_prices.return_value = {}
        monitor.data_layer = dl
        assert monitor._check_stock("000001.SZ", "2026-07-20") is None

    def test_no_prev_close(self, monitor):
        dl = make_dl(price=10.65, prev_close=None)
        monitor.data_layer = dl
        assert monitor._check_stock("000001.SZ", "2026-07-20", price=10.65) is None

    def test_surge_detection(self, monitor):
        monitor.data_layer = make_dl()
        result = monitor._check_stock("000001.SZ", "2026-07-20", price=10.65)
        assert result is not None
        assert result["type"] == "surge"
        assert result["change_pct"] == 6.5

    def test_plunge_detection(self, monitor):
        monitor.data_layer = make_dl(price=9.3)
        result = monitor._check_stock("000001.SZ", "2026-07-20", price=9.3)
        assert result is not None
        assert result["type"] == "plunge"
        assert result["change_pct"] == -7.0

    def test_volume_ratio_is_none_not_fake_2(self, monitor):
        """ST-02/S-21 回归: 无真实量比数据源时 volume_ratio 必须为 None + 显式标注, 不得硬编码 2.0"""
        monitor.data_layer = make_dl()
        result = monitor._check_stock("000001.SZ", "2026-07-20", price=10.65)
        assert result["volume_ratio"] is None
        assert result["volume_ratio_available"] is False

    def test_no_signal_within_bounds(self, monitor):
        monitor.data_layer = make_dl(price=10.05)
        assert monitor._check_stock("000001.SZ", "2026-07-20", price=10.05) is None

    def test_exactly_at_threshold_no_signal(self, monitor):
        monitor.data_layer = make_dl(price=10.5)  # 恰好 +5.0%, 不满足 > 5
        assert monitor._check_stock("000001.SZ", "2026-07-20", price=10.5) is None

    def test_price_fetched_when_not_passed(self, monitor):
        """不传 price 时逐股调 get_realtime_prices"""
        dl = make_dl()
        monitor.data_layer = dl
        result = monitor._check_stock("000001.SZ", "2026-07-20")
        assert result is not None
        dl.get_realtime_prices.assert_called_with(["000001.SZ"])

    def test_error_handling(self, monitor):
        dl = MagicMock()
        dl.get_realtime_prices.side_effect = Exception("source down")
        monitor.data_layer = dl
        assert monitor._check_stock("000001.SZ", "2026-07-20") is None


class TestScan:
    def test_scan_batch_fetches_prices_once(self, monitor):
        """scan 批量取价 (一次 get_realtime_prices), 不再逐股 snapshot_intraday"""
        dl = make_dl()
        monitor.data_layer = dl
        with patch.object(monitor, "is_trading_hours", return_value=True):
            signals = monitor.scan(["000001.SZ"], "2026-07-20")
        assert len(signals) == 1
        assert signals[0]["type"] == "surge"
        dl.get_realtime_prices.assert_called_once_with(["000001.SZ"])

    def test_scan_outside_trading_hours_empty(self, monitor):
        monitor.data_layer = make_dl()
        with patch.object(monitor, "is_trading_hours", return_value=False):
            assert monitor.scan(["000001.SZ"], "2026-07-20") == []

    def test_scan_price_fetch_error_returns_empty(self, monitor):
        dl = MagicMock()
        dl.get_realtime_prices.side_effect = Exception("all sources down")
        monitor.data_layer = dl
        with patch.object(monitor, "is_trading_hours", return_value=True):
            assert monitor.scan(["000001.SZ"], "2026-07-20") == []

    def test_scan_reachable_surge_and_plunge(self, monitor):
        """验收: mock data_layer 断言 surge/plunge 判定可达 (原实现恒空)"""
        dl = MagicMock()
        dl.get_realtime_prices.return_value = {"A": 11.0, "B": 9.0}
        dl.get_history.return_value = SimpleNamespace(
            klines=[SimpleNamespace(date="2026-07-17", close=10.0)])
        monitor.data_layer = dl
        with patch.object(monitor, "is_trading_hours", return_value=True):
            signals = monitor.scan(["A", "B"], "2026-07-20")
        types = {s["type"] for s in signals}
        assert types == {"surge", "plunge"}
