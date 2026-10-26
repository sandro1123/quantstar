"""信号生成单元测试"""
import pytest
from strategy_layer.signals.generator import SignalGenerator, TradeSignal
from strategy_layer.selection.selector import SelectionResult
from strategy_layer.aggregation.aggregator import FactorDaily


def test_signal_generation():
    gen = SignalGenerator()
    selection = SelectionResult(
        date="2026-07-17",
        candidates=[
            FactorDaily(code="000001", date="2026-07-17", composite_score=1.5, rank=1),
            FactorDaily(code="000002", date="2026-07-17", composite_score=0.5, rank=2),
        ],
    )
    signals = gen.generate(selection)
    assert len(signals) == 2
    assert all(s.action == "buy" for s in signals)
    assert all(0 <= s.strength <= 1 for s in signals)


def test_signal_no_duplicates():
    gen = SignalGenerator()
    selection = SelectionResult(
        date="2026-07-17",
        candidates=[FactorDaily(code="000001", date="2026-07-17", composite_score=1.0, rank=1)],
    )
    signals = gen.generate(selection, current_positions={"000001": {}})
    assert len(signals) == 1  # hold signal for existing portfolio stock
    assert signals[0].action == "hold"


def test_signal_empty():
    gen = SignalGenerator()
    selection = SelectionResult(date="2026-07-17", candidates=[])
    signals = gen.generate(selection)
    assert len(signals) == 0


def test_signal_new_fields():
    gen = SignalGenerator()
    selection = SelectionResult(
        date="2026-07-17",
        candidates=[FactorDaily(code="000001", date="2026-07-17", composite_score=1.5, rank=1)],
    )
    signals = gen.generate(selection)
    assert len(signals) == 1
    s = signals[0]
    assert s.signal_id != ""
    assert len(s.signal_id) == 8
    assert s.direction == "long"
    assert 0 <= s.magnitude <= 1
    assert 0 <= s.confidence <= 1
    assert s.ttl == 1
    assert s.source_model == "factor_engine"
    assert s.expected_cost == 0.16  # BUY_COST(0.03%) + SELL_COST(0.13%) = 0.16%


def test_signal_id_unique():
    gen = SignalGenerator()
    selection = SelectionResult(
        date="2026-07-17",
        candidates=[
            FactorDaily(code="000001", date="2026-07-17", composite_score=1.5, rank=1),
            FactorDaily(code="000002", date="2026-07-17", composite_score=0.5, rank=2),
        ],
    )
    signals = gen.generate(selection)
    ids = [s.signal_id for s in signals]
    assert len(ids) == len(set(ids))


def test_signal_direction_matches_action():
    gen = SignalGenerator()
    selection = SelectionResult(
        date="2026-07-17",
        candidates=[FactorDaily(code="000001", date="2026-07-17", composite_score=1.5, rank=1)],
    )
    signals = gen.generate(selection)
    for s in signals:
        if s.action == "buy":
            assert s.direction == "long"
        elif s.action == "sell":
            assert s.direction == "close"


def test_signal_expected_cost():
    gen = SignalGenerator()
    selection = SelectionResult(
        date="2026-07-17",
        candidates=[FactorDaily(code="000001", date="2026-07-17", composite_score=1.5, rank=1)],
    )
    signals = gen.generate(selection)
    assert len(signals) == 1
    s = signals[0]
    assert s.expected_cost == 0.16
    assert s.action == "buy"


def test_signal_sell_no_expected_cost():
    gen = SignalGenerator()
    selection = SelectionResult(
        date="2026-07-17",
        candidates=[FactorDaily(code="000002", date="2026-07-17", composite_score=1.0, rank=1)],
    )
    signals = gen.generate(selection, current_positions={"000001": {}})
    sell_signals = [s for s in signals if s.action == "sell"]
    assert len(sell_signals) == 1
    assert sell_signals[0].expected_cost == 0.0  # sell signals don't have expected_cost set
