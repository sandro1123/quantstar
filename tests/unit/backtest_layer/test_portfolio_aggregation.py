"""run_portfolio_backtest 组合聚合回归测试

修复前: 线性加权 Σr_i·w_i(缺复利) + max_drawdown=min(各股)(错误),
win_rate 从个股反推。
修复后: 归一化各股 equity_curve → 组合每日净值曲线,
复利回报/回撤/夏普均从组合曲线计算。
"""
import pytest
from unittest.mock import MagicMock

from backtest_layer import BacktestLayer
from backtest_layer.core.models import BacktestReport


def _report(curve, total_return=None, trades=4, win_rate=0.5):
    navs = [v for _, v in curve]
    total_return = total_return if total_return is not None else navs[-1] / navs[0] - 1.0
    from backtest_layer.core.metrics import Metrics
    m = Metrics.calc(navs)
    return BacktestReport(
        mode="hold",
        date_range=("2026-01-01", "2026-01-03"),
        total_return=total_return,
        annual_return=m.annual_return,
        sharpe=m.sharpe,
        max_drawdown=m.max_drawdown,
        win_rate=win_rate,
        total_trades=trades,
        benchmark_return=0.0,
        excess_return=total_return,
        equity_curve=[{"date": d, "nav": v} for d, v in curve],
    )


@pytest.fixture
def layer():
    return BacktestLayer(data_layer=MagicMock(), strategy_layer=MagicMock())


def test_portfolio_aggregates_compound_curve(layer, monkeypatch):
    """组合曲线 = 各股归一化净值加权; 回报 = 组合曲线末-1 (复利)。"""
    report_a = _report([(1, 100.0), (2, 110.0), (3, 120.0)])  # +20%
    report_b = _report([(1, 100.0), (2, 115.0), (3, 130.0)])  # +30%

    def fake_run(input_data):
        return report_a if input_data.codes == ["A"] else report_b

    monkeypatch.setattr(layer, "run", fake_run)

    result = layer.run_portfolio_backtest(codes=["A", "B"], weights=[0.5, 0.5],
                                          date_range=("2026-01-01", "2026-01-03"))
    # 组合: 0.5*1.0 + 0.5*1.0 = 1.0 → 0.5*1.2 + 0.5*1.3 = 1.25
    assert abs(result.total_return - 0.25) < 1e-6
    assert len(result.equity_curve) == 3
    assert 'date' in result.equity_curve[0]


def test_portfolio_max_drawdown_from_curve_not_min(layer, monkeypatch):
    """max_drawdown 应从组合曲线计算, 而非 min(各股)。"""
    # A 冲到 1.5 再跌回 1.2 (dd=20%), B 几乎持平 (dd≈1%)
    report_a = _report([(1, 100.0), (2, 150.0), (3, 120.0)])
    report_b = _report([(1, 100.0), (2, 101.0), (3, 102.0)])

    def fake_run(input_data):
        return report_a if input_data.codes == ["A"] else report_b

    monkeypatch.setattr(layer, "run", fake_run)

    result = layer.run_portfolio_backtest(codes=["A", "B"], weights=[0.5, 0.5],
                                          date_range=("2026-01-01", "2026-01-03"))
    # 组合: [1.0, 1.255, 1.11]; peak=1.255, dd=(1.255-1.11)/1.255≈0.11554
    assert result.max_drawdown > 0.10
    assert result.max_drawdown < 0.20
    assert result.max_drawdown != 0.20  # 不等同于 min(各股 20%)


def test_portfolio_weights_are_normalized(layer, monkeypatch):
    """weights 应归一化 (总和化为 1)。"""
    report = _report([(1, 100.0), (2, 120.0)])  # +20%

    def fake_run(input_data):
        return report

    monkeypatch.setattr(layer, "run", fake_run)

    result = layer.run_portfolio_backtest(codes=["A", "B"], weights=[2.0, 2.0],
                                          date_range=("2026-01-01", "2026-01-02"))
    # 归一化后 w=[0.5,0.5], 组合曲线 [1.0, 1.2] → 回报 20%
    assert abs(result.total_return - 0.20) < 1e-6


def test_portfolio_win_rate_weighted(layer, monkeypatch):
    """win_rate 应为各股成功数加权合计 / 总交易数。"""
    report_a = _report([(1, 100.0), (2, 120.0)], trades=10, win_rate=0.8)
    report_b = _report([(1, 100.0), (2, 95.0)], trades=10, win_rate=0.1)

    def fake_run(input_data):
        return report_a if input_data.codes == ["A"] else report_b

    monkeypatch.setattr(layer, "run", fake_run)

    result = layer.run_portfolio_backtest(codes=["A", "B"], weights=[0.5, 0.5],
                                          date_range=("2026-01-01", "2026-01-02"))
    assert result.total_trades == 20
    assert abs(result.win_rate - (0.8 * 10 + 0.1 * 10) / 20) < 1e-6


def test_portfolio_rejects_weight_mismatch(layer):
    result = layer.run_portfolio_backtest(codes=["A", "B"], weights=[0.5],
                                          date_range=("2026-01-01", "2026-01-02"))
    assert result is None  # catch_quantstar_error → fallback_return=None


def test_portfolio_rejects_empty_codes(layer):
    result = layer.run_portfolio_backtest(codes=[], date_range=("2026-01-01", "2026-01-02"))
    assert result is None  # catch_quantstar_error → fallback_return=None