"""BT-08: regime_split 接线单元测试 — 全覆盖/空表/部分覆盖/报告携带"""
import pytest
from backtest_layer.core.models import BacktestReport
from backtest_layer.validation.regime_split import RegimeSplit, attach_regime_breakdown


class FakeStrategyLayer:
    """模拟 strategy_layer.get_daily_regime 读 API"""
    def __init__(self, rows: list[dict]):
        self._rows = rows

    def get_daily_regime(self, date_range):
        if isinstance(date_range, (tuple, list)):
            start, end = date_range[0], date_range[1]
            return [r for r in self._rows if start <= r["date"] <= end]
        return [r for r in self._rows if r["date"] == str(date_range)]


def _make_report(equity_curve, trade_log=None):
    return BacktestReport(
        mode="hold", date_range=("2026-08-01", "2026-08-05"),
        total_return=0.0, annual_return=0.0, sharpe=0.0, max_drawdown=0.0,
        win_rate=0.0, total_trades=0,
        equity_curve=equity_curve, trade_log=trade_log or [],
    )


class TestRegimeSplitFullCoverage:
    """① 全覆盖区间 → 拆分正确"""

    def test_full_coverage_split(self):
        curve = [
            {"date": "2026-08-01", "nav": 100.0},
            {"date": "2026-08-02", "nav": 110.0},
            {"date": "2026-08-03", "nav": 105.0},
            {"date": "2026-08-04", "nav": 95.0},
            {"date": "2026-08-05", "nav": 90.0},
        ]
        rows = [
            {"date": "2026-08-01", "regime": "bull"},
            {"date": "2026-08-02", "regime": "bull"},
            {"date": "2026-08-03", "regime": "neutral"},
            {"date": "2026-08-04", "regime": "bear"},
            {"date": "2026-08-05", "regime": "bear"},
        ]
        report = _make_report(curve)
        result = RegimeSplit().split_report(report, FakeStrategyLayer(rows))

        assert result["available"] is True
        assert result["coverage"] == "5/5"
        assert result["coverage_pct"] == 1.0
        assert "note" not in result  # 全覆盖无注记
        segs = result["segments"]
        assert "bull" in segs and "bear" in segs and "neutral" in segs
        assert segs["bull"]["days"] == 2
        assert segs["bull"]["return"] == pytest.approx(0.1, abs=0.01)
        assert segs["bear"]["return"] < 0
        assert segs["bear"]["max_drawdown"] > 0

    def test_full_coverage_trade_attribution(self):
        curve = [
            {"date": "2026-08-01", "nav": 100.0},
            {"date": "2026-08-02", "nav": 110.0},
        ]
        rows = [
            {"date": "2026-08-01", "regime": "bull"},
            {"date": "2026-08-02", "regime": "bull"},
        ]
        trades = [{"entry_date": "2026-08-01", "pnl": 100}]
        report = _make_report(curve, trade_log=trades)
        result = RegimeSplit().split_report(report, FakeStrategyLayer(rows))
        assert result["segments"]["bull"]["trades"] == 1


class TestRegimeSplitEmptyTable:
    """② 空表 → 优雅降级标注"""

    def test_empty_regime_table(self):
        curve = [
            {"date": "2026-08-01", "nav": 100.0},
            {"date": "2026-08-02", "nav": 105.0},
        ]
        report = _make_report(curve)
        result = RegimeSplit().split_report(report, FakeStrategyLayer([]))

        assert result["available"] is False
        assert "0/2" in result["reason"]
        assert result["coverage"] == "0/2"

    def test_no_strategy_layer(self):
        curve = [{"date": "2026-08-01", "nav": 100.0}]
        report = _make_report(curve)
        result = RegimeSplit().split_report(report, None)
        assert result["available"] is False
        assert "strategy_layer" in result["reason"]

    def test_empty_equity_curve(self):
        report = _make_report([])
        result = RegimeSplit().split_report(report, FakeStrategyLayer([]))
        assert result["available"] is False

    def test_float_equity_curve_no_date_axis(self):
        """signal 模式纯 float 曲线 → 无日期轴降级"""
        report = _make_report([100.0, 105.0])
        result = RegimeSplit().split_report(report, FakeStrategyLayer([]))
        assert result["available"] is False
        assert "日期轴" in result["reason"]


class TestRegimeSplitPartialCoverage:
    """③ 部分覆盖 → 部分拆分 + 覆盖率"""

    def test_partial_coverage(self):
        curve = [
            {"date": "2026-08-01", "nav": 100.0},
            {"date": "2026-08-02", "nav": 110.0},
            {"date": "2026-08-03", "nav": 108.0},
            {"date": "2026-08-04", "nav": 106.0},
        ]
        # 只覆盖前两天
        rows = [
            {"date": "2026-08-01", "regime": "bull"},
            {"date": "2026-08-02", "regime": "bull"},
        ]
        report = _make_report(curve)
        result = RegimeSplit().split_report(report, FakeStrategyLayer(rows))

        assert result["available"] is True
        assert result["coverage"] == "2/4"
        assert result["coverage_pct"] == pytest.approx(0.5)
        assert "note" in result
        assert "部分覆盖" in result["note"]
        assert result["segments"]["bull"]["days"] == 2


class TestAttachRegimeBreakdown:
    """④ BacktestReport 携带 regime_breakdown"""

    def test_attach_writes_to_report(self):
        curve = [
            {"date": "2026-08-01", "nav": 100.0},
            {"date": "2026-08-02", "nav": 110.0},
        ]
        rows = [
            {"date": "2026-08-01", "regime": "bull"},
            {"date": "2026-08-02", "regime": "bull"},
        ]
        report = _make_report(curve)
        attach_regime_breakdown(report, FakeStrategyLayer(rows))
        assert report.regime_breakdown["available"] is True
        assert "bull" in report.regime_breakdown["segments"]

    def test_attach_degrades_on_exception(self):
        curve = [{"date": "2026-08-01", "nav": 100.0}]
        report = _make_report(curve)

        class BrokenLayer:
            def get_daily_regime(self, dr):
                raise RuntimeError("db down")

        attach_regime_breakdown(report, BrokenLayer())
        assert report.regime_breakdown["available"] is False
        assert "异常" in report.regime_breakdown["reason"]

    def test_attach_no_strategy_layer(self):
        curve = [{"date": "2026-08-01", "nav": 100.0}]
        report = _make_report(curve)
        attach_regime_breakdown(report, None)
        assert report.regime_breakdown["available"] is False
