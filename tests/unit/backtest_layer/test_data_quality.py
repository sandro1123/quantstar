"""DataQuality + Benchmark 增强 单元测试"""
import pytest
from unittest.mock import MagicMock
from backtest_layer.core.data_quality import DataQualityCheck, DataQualityResult
from backtest_layer.core.benchmark import Benchmark


class TestDataQualityCheck:
    def test_check_empty_codes(self):
        result = DataQualityCheck.check([], ("2026-01-01", "2026-12-31"), None)
        assert isinstance(result, DataQualityResult)
        assert result.passed is False
        assert "空" in result.warnings[0]

    def test_check_invalid_date_range(self):
        result = DataQualityCheck.check(["000001"], ("2026-12-31", "2026-01-01"), None)
        assert result.passed is False

    def test_check_none_data_layer(self):
        result = DataQualityCheck.check(["000001"], ("2026-01-01", "2026-12-31"), None)
        assert result.passed is False
        assert "未初始化" in result.warnings[0]

    def test_check_with_mock_data_layer(self):
        dl = MagicMock()
        dl.snapshot.return_value = None
        result = DataQualityCheck.check(["000001"], ("2026-01-01", "2026-12-31"), dl)
        assert result.passed is False
        assert result.checked_codes == 1
        assert "000001" in result.missing_codes

    def test_check_with_valid_data(self):
        dl = MagicMock()
        dl.snapshot.return_value = MagicMock()
        result = DataQualityCheck.check(["000001"], ("2026-01-01", "2026-12-31"), dl)
        assert result.passed is True
        assert result.missing_codes == []


class TestBenchmarkEnhanced:
    def test_fetch_benchmark_curve_no_data_layer(self):
        b = Benchmark()
        curve = b.fetch_benchmark_curve(("2026-01-01", "2026-12-31"))
        assert curve == []

    def test_fetch_benchmark_curve_with_data_layer(self):
        # BT-04: 曲线数据源改为 DL-23 get_index_daily
        # (指数收盘价归一化净值); 旧的 snapshot导航 nav=1.0 假曲线已删除
        dl = MagicMock()
        dl.get_index_daily.return_value = [
            {"date": "2026-01-02", "close": 100.0, "name": "CSI300"},
            {"date": "2026-01-03", "close": 105.0, "name": "CSI300"},
        ]
        b = Benchmark()
        curve = b.fetch_benchmark_curve(("2026-01-01", "2026-06-30"), dl)
        dl.get_index_daily.assert_called_once_with(
            "000300.SH", start_date="2026-01-01", end_date="2026-06-30")
        assert len(curve) == 2
        assert curve[0]["nav"] == pytest.approx(1.0)
        assert curve[-1]["nav"] == pytest.approx(1.05)

    def test_fetch_benchmark_curve_no_api_degrades_empty(self):
        # BT-04: 数据层无 get_index_daily 读 API → 空曲线 (诚实降级, 不造假)
        class NoAPILayer:
            pass
        b = Benchmark()
        assert b.fetch_benchmark_curve(("2026-01-01", "2026-06-30"), NoAPILayer()) == []

    def test_align_to_strategy(self):
        b = Benchmark()
        bench = [{"date": "2026-01-01", "nav": 1.0}, {"date": "2026-01-02", "nav": 1.01}]
        strat = [{"date": "2026-01-01", "nav": 100000}, {"date": "2026-01-02", "nav": 101000}]
        b_navs, s_navs = b.align_to_strategy(bench, strat)
        assert len(b_navs) == 2
        assert len(s_navs) == 2

    def test_align_empty(self):
        b = Benchmark()
        b_navs, s_navs = b.align_to_strategy([], [{"date": "x"}])
        assert b_navs == []

    def test_calc_benchmark_metrics(self):
        b = Benchmark()
        curve = [{"date": f"2026-01-{i:02d}", "nav": 1.0 + i * 0.001} for i in range(1, 22)]
        metrics = b.calc_benchmark_metrics(curve)
        assert "total_return" in metrics
        assert "annual_return" in metrics
        assert "max_drawdown" in metrics

    def test_comparison_summary(self):
        b = Benchmark()
        sm = {"total_return": 0.15, "sharpe": 1.5}
        bm = {"total_return": 0.10, "max_drawdown": 0.05}
        summary = b.comparison_summary(sm, bm)
        assert summary["excess_return"] == 0.05
