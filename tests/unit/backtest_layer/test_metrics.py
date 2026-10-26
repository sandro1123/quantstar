"""Metrics 单元测试"""
import pytest
from backtest_layer.core.metrics import Metrics
from backtest_layer._models import BacktestMetrics


def test_calc_sharpe():
    curve = [100, 101, 102, 103, 104, 105]
    result = Metrics.calc(curve)
    assert isinstance(result, BacktestMetrics)
    assert hasattr(result, 'sharpe')
    assert hasattr(result, 'max_drawdown')
    assert hasattr(result, 'annual_return')


def test_calc_max_drawdown():
    curve = [100, 110, 105, 102, 108]
    result = Metrics.calc(curve)
    assert result.max_drawdown > 0


# ---- BT-06 假指标修复回归 ----

def test_geometric_param_now_effective():
    """BT-06: 原二次覆盖把 annual_return 硬编码 252, trading_days/geometric 参数恒无效;
    修复后参数真生效。完整曲线上 geometric 与算术 total_return 数值一致 (伸缩积),
    真正的参数敏感性在 annual_return 的 trading_days。"""
    curve = [100, 120, 90, 108]
    a = Metrics.calc(curve, geometric=False, trading_days=126)
    b = Metrics.calc(curve, geometric=False, trading_days=252)
    assert a.total_return == pytest.approx(0.08, abs=1e-4)
    # trading_days 不再被硬编码 252 覆盖
    assert a.annual_return != b.annual_return
    # geometric 分支保留且与算术一致 (完整曲线伸缩积)
    g = Metrics.calc(curve, geometric=True)
    assert g.total_return == pytest.approx(a.total_return, abs=1e-4)


def test_sqn_uses_trade_pnl_not_daily_returns():
    """SQN 必须用每笔交易 pnl; 无 trade_pnls 时显式 None 不可算"""
    curve = [100, 101, 102, 103]
    no_trades = Metrics.calc(curve)
    assert no_trades.sqn is None  # 宁可没有, 不可是假的

    pnls = [100.0, -50.0, 200.0, 80.0]
    with_trades = Metrics.calc(curve, trade_pnls=pnls)
    assert with_trades.sqn is not None
    # 手工核对: mean=82.5, std=sqrt(((17.5)^2+(132.5)^2+(117.5)^2+(2.5)^2)/4)
    import math
    mean = sum(pnls) / len(pnls)
    std = math.sqrt(sum((p - mean) ** 2 for p in pnls) / len(pnls))
    expected = mean / std * math.sqrt(len(pnls))
    assert with_trades.sqn == pytest.approx(round(expected, 4), abs=1e-4)


def test_capture_ratio_requires_benchmark():
    """无基准时 capture_ratio 显式 None (原自指算法已删), 有基准时真算"""
    curve = [100, 105, 103, 108]
    assert Metrics.calc(curve).capture_ratio is None

    # 策略日收益 vs 基准日收益
    bench = [0.02, -0.01, 0.03]  # 对应 curve 的 3 个日收益区间
    result = Metrics.calc(curve, benchmark_returns=bench)
    assert result.capture_ratio is not None


def test_dsr_num_trials_passed_through():
    """num_trials 从调参记录真实传入 (原恒 1 → 校正从未生效)"""
    curve = [100, 102, 101, 104, 103, 106]
    single = Metrics.calc(curve, num_trials=1)
    multi = Metrics.calc(curve, num_trials=50)
    # 多试次校正后 deflated_sharpe 应更低 (expected_max_sr 抬升)
    assert multi.deflated_sharpe <= single.deflated_sharpe
