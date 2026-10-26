"""绩效仪表盘单元测试"""
import numpy as np
import pytest

from strategy_layer.reporting.dashboard import PerformanceDashboardBuilder


def test_metrics_with_known_returns():
    builder = PerformanceDashboardBuilder()
    returns = [0.001, 0.002, -0.001, 0.003, 0.001, -0.002, 0.001, 0.002, 0.0005, -0.001]
    dashboard = builder.build(date="2026-07-17", returns=returns)

    assert dashboard.date == "2026-07-17"
    assert dashboard.metrics.total_trades == 10
    assert dashboard.metrics.sharpe > 0
    assert dashboard.metrics.annualized_return > 0
    assert dashboard.metrics.max_drawdown > 0


def test_rolling_6m_metrics():
    builder = PerformanceDashboardBuilder()
    np.random.seed(42)
    returns = list(np.random.randn(252) * 0.002)
    dashboard = builder.build(date="2026-07-17", returns=returns)

    assert len(dashboard.rolling) > 0
    for r in dashboard.rolling:
        assert r.sharpe_6m != 0.0
        assert r.max_dd_6m >= 0.0


def test_insufficient_data():
    builder = PerformanceDashboardBuilder()
    returns = [0.001, 0.002]
    dashboard = builder.build(date="2026-07-17", returns=returns)

    assert len(dashboard.warnings) == 1
    assert "Insufficient data" in dashboard.warnings[0]
    assert dashboard.metrics.total_trades == 0
    assert dashboard.metrics.sharpe == 0.0


def test_with_benchmark():
    builder = PerformanceDashboardBuilder()
    returns = [0.001, 0.002, -0.001, 0.003, 0.001]
    benchmark = [0.0005, 0.001, 0.0005, 0.001, 0.0005]
    dashboard = builder.build(date="2026-07-17", returns=returns, benchmark_returns=benchmark)

    assert dashboard.metrics.benchmark_return > 0
    assert dashboard.metrics.excess_return != 0.0


def test_win_rates():
    builder = PerformanceDashboardBuilder()
    returns = [0.001, 0.002, -0.001, 0.003, 0.001]
    win_rates = [0.6, 0.7, 0.3, 0.8, 0.6]
    dashboard = builder.build(date="2026-07-17", returns=returns, win_rates=win_rates)

    assert dashboard.metrics.win_rate > 0


def test_all_negative_returns():
    builder = PerformanceDashboardBuilder()
    returns = [-0.001, -0.002, -0.001, -0.003, -0.001, -0.002]
    dashboard = builder.build(date="2026-07-17", returns=returns)

    assert dashboard.metrics.annualized_return < 0
    assert dashboard.metrics.win_rate == 0.0
    assert dashboard.metrics.profit_factor == 0.0


def test_zero_std_returns():
    builder = PerformanceDashboardBuilder()
    returns = [0.001] * 10
    dashboard = builder.build(date="2026-07-17", returns=returns)

    assert dashboard.metrics.sharpe == 0.0
    assert dashboard.metrics.sortino == 0.0