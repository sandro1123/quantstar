"""HTML Report Builder 单元测试"""
import pytest
import tempfile
import os
from backtest_layer.core.models import BacktestReport
from backtest_layer.results.report import ReportStore, HTMLReportBuilder


class TestHTMLReportBuilder:
    def _make_report(self):
        return BacktestReport(
            mode="hold",
            date_range=("2026-01-01", "2026-06-30"),
            total_return=0.15,
            annual_return=0.30,
            sharpe=1.5,
            max_drawdown=0.05,
            win_rate=0.6,
            total_trades=10,
            benchmark_return=0.10,
            excess_return=0.05,
            equity_curve=[{"date": "2026-01-01", "nav": 100000}, {"date": "2026-06-30", "nav": 115000}],
            trade_log=[{"code": "000001", "action": "buy", "date": "2026-01-01", "price": 15.0, "shares": 100}],
            one_line_verdict="收益 15.0%, 夏普 1.50, 策略表现",
        )

    def test_build_basic(self):
        report = self._make_report()
        html = HTMLReportBuilder.build(report)
        assert "<!DOCTYPE html>" in html
        assert "回测报告" in html
        assert "15.00%" in html

    def test_build_with_benchmark(self):
        report = self._make_report()
        comparison = {"strategy_return": 0.15, "benchmark_return": 0.10, "excess_return": 0.05}
        html = HTMLReportBuilder.build(report, comparison)
        assert "基准对比" in html

    def test_build_empty_trades(self):
        report = self._make_report()
        report.trade_log = []
        html = HTMLReportBuilder.build(report)
        assert "无交易记录" in html

    def test_save_html(self):
        report = self._make_report()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test_report.html")
            result = HTMLReportBuilder.save_html(report, path)
            assert os.path.exists(result)
            with open(result, "r") as f:
                content = f.read()
            assert "回测报告" in content


class TestReportStore:
    def test_save_and_load(self):
        report = BacktestReport(
            mode="test", date_range=("2026-01-01", "2026-06-30"),
            total_return=0.1, annual_return=0.2, sharpe=1.0,
            max_drawdown=0.05, win_rate=0.6, total_trades=5,
            benchmark_return=0.08, excess_return=0.02,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ReportStore(base_dir=tmpdir)
            path = store.save(report, "test.json")
            assert os.path.exists(path)
            loaded = store.load("test.json")
            assert loaded["mode"] == "test"
            assert loaded["total_return"] == 0.1
