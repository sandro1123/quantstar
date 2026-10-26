"""Signal Mode 单元测试"""
import pytest
from backtest_layer.core.broker import Broker
from backtest_layer.core.models import CostConfig
from backtest_layer.modes.signal import SignalMode


def test_signal_mode_buy():
    broker = Broker(CostConfig(initial_capital=100000))
    mode = SignalMode(broker)
    signals = [{"code": "000001", "signal": "buy", "date": "2026-07-17"}]
    prices = {"000001": {"2026-07-16": 15.0, "2026-07-17": 15.0}}
    result = mode.run(signals, prices)
    assert result["trade_count"] == 1
    assert "000001" in broker.positions


def test_signal_mode_sell():
    broker = Broker(CostConfig(initial_capital=100000))
    broker.buy("000001", 15.0, 1000, "2026-07-16")
    mode = SignalMode(broker)
    signals = [{"code": "000001", "signal": "sell", "date": "2026-07-17"}]
    prices = {"000001": {"2026-07-16": 15.0, "2026-07-17": 16.0}}
    result = mode.run(signals, prices)
    assert "000001" not in broker.positions


def test_signal_mode_fills_at_signal_date_price_not_last_price():
    """旧实现所有信号用整段最后一个价格成交 → 买卖价相同, 盈亏只剩手续费。"""
    broker = Broker(CostConfig(initial_capital=100000, slippage_rate=0.0))
    mode = SignalMode(broker)
    prices = {"600000": {"2026-09-01": 10.0, "2026-09-02": 10.5, "2026-09-03": 11.0,
                         "2026-09-04": 11.5}}
    signals = [{"code": "600000", "signal": "buy", "date": "2026-09-02"},
               {"code": "600000", "signal": "sell", "date": "2026-09-04"}]
    result = mode.run(signals, prices)
    buy, sell = result["trade_log"]
    assert buy["price"] == pytest.approx(10.5, rel=1e-3)
    assert sell["price"] == pytest.approx(11.5, rel=1e-3)
    assert sell["shares"] == buy["shares"] > 0  # 旧实现卖出后读取 → 恒 0
    assert sell["pnl"] > 0


def test_signal_mode_undated_prices_never_use_future_price():
    broker = Broker(CostConfig(initial_capital=100000))
    mode = SignalMode(broker)
    result = mode.run([{"code": "000001", "signal": "buy", "date": "2026-07-17"}],
                      {"000001": [10.0, 15.0, 20.0]})
    assert result["trade_count"] == 0 and result["skipped_signals"] == 1
    # 信号自带价格时可成交
    result = mode.run([{"code": "000001", "signal": "buy", "date": "2026-07-17", "price": 10.0}],
                      {"000001": [10.0, 15.0, 20.0]})
    assert result["trade_count"] == 1


def test_signal_mode_respects_limit_up_with_pre_close():
    broker = Broker(CostConfig(initial_capital=100000))
    mode = SignalMode(broker)
    prices = {"600000": {"2026-09-01": 10.0, "2026-09-02": 11.0}}  # 一字涨停
    result = mode.run([{"code": "600000", "signal": "buy", "date": "2026-09-02"}], prices)
    assert result["trade_count"] == 0


def _engine_run(signals, price_data):
    from backtest_layer.core.engine import BacktestEngine
    from backtest_layer.core.models import BacktestInput
    cost = CostConfig(initial_capital=100000)
    inp = BacktestInput(mode="signal", codes=["600000"], date_range=("2026-09-01", "2026-09-10"),
                        cost=cost, strategy_params={"signals": signals, "price_data": price_data})
    return BacktestEngine(cost=cost).run(inp)


def test_engine_signal_mode_metrics_use_full_curve():
    prices = {"600000": {"2026-09-01": 10.0, "2026-09-02": 10.5, "2026-09-03": 10.2,
                         "2026-09-04": 10.8, "2026-09-07": 10.4, "2026-09-08": 11.0}}
    signals = [{"code": "600000", "signal": "buy", "date": "2026-09-02"}] + [
        {"code": "600000", "signal": "hold", "date": d}
        for d in ("2026-09-03", "2026-09-04", "2026-09-07", "2026-09-08")]
    report = _engine_run(signals, prices)
    assert report.max_drawdown != 0.0  # 旧实现只取第一个点 → 恒 0


def test_engine_signal_mode_empty_signals_does_not_crash():
    report = _engine_run([], {})
    assert report.total_trades == 0
