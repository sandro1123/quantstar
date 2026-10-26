"""Tests for TradeSignal price fields — entry, stop_loss, take_profit via ATR"""
import pytest
from strategy_layer.signals.generator import SignalGenerator, TradeSignal
from strategy_layer.selection.selector import SelectionResult
from strategy_layer.aggregation.aggregator import FactorDaily


def test_signal_has_price_fields():
    gen = SignalGenerator()
    selection = SelectionResult(
        date="2026-07-17",
        candidates=[FactorDaily(code="000001", date="2026-07-17", composite_score=1.5, rank=1)],
    )
    signals = gen.generate(selection, stock_prices={"000001": 10.0}, atr_values={"000001": 0.5})
    assert len(signals) == 1
    s = signals[0]
    assert s.entry_low > 0
    assert s.entry_high > s.entry_low
    assert s.stop_loss > 0
    assert s.take_profit > 0


def test_signal_stop_loss_uses_atr():
    gen = SignalGenerator()
    selection = SelectionResult(
        date="2026-07-17",
        candidates=[FactorDaily(code="000001", date="2026-07-17", composite_score=1.5, rank=1)],
    )
    signals = gen.generate(selection, stock_prices={"000001": 10.0}, atr_values={"000001": 0.5})
    s = signals[0]
    assert s.stop_loss == pytest.approx(10.0 - 2 * 0.5, abs=0.01)
    assert s.take_profit == pytest.approx(10.0 + 3 * 0.5, abs=0.01)


def test_signal_no_price_fields_without_prices():
    gen = SignalGenerator()
    selection = SelectionResult(
        date="2026-07-17",
        candidates=[FactorDaily(code="000001", date="2026-07-17", composite_score=1.5, rank=1)],
    )
    signals = gen.generate(selection)
    s = signals[0]
    assert s.entry_low == 0.0
    assert s.entry_high == 0.0
    assert s.stop_loss == 0.0
    assert s.take_profit == 0.0


def test_signal_sell_has_no_entry_fields():
    gen = SignalGenerator()
    selection = SelectionResult(
        date="2026-07-17",
        candidates=[FactorDaily(code="000002", date="2026-07-17", composite_score=1.0, rank=1)],
    )
    signals = gen.generate(selection, current_positions={"000001": {}},
                           stock_prices={"000001": 10.0})
    sell = [s for s in signals if s.action == "sell"]
    assert len(sell) == 1
    assert sell[0].entry_low == 0.0


def test_signal_atr_ratio():
    gen = SignalGenerator()
    selection = SelectionResult(
        date="2026-07-17",
        candidates=[FactorDaily(code="000001", date="2026-07-17", composite_score=1.5, rank=1)],
    )
    signals = gen.generate(selection, stock_prices={"000001": 20.0}, atr_values={"000001": 1.0})
    s = signals[0]
    risk = s.entry_high - s.stop_loss
    reward = s.take_profit - s.entry_high
    assert reward == pytest.approx(risk, abs=0.01)
