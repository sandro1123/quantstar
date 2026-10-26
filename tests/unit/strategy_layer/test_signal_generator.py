"""Tests for SignalGenerator"""
from strategy_layer.signals.generator import SignalGenerator, TradeSignal
from strategy_layer.selection.selector import SelectionResult
from strategy_layer.aggregation.aggregator import FactorDaily


def _make_fd(code: str, score: float = 0.5, rank: int = 1) -> FactorDaily:
    return FactorDaily(code=code, date="2026-07-24", composite_score=score,
                       factor_scores={}, rank=rank, sector="")


class TestSignalGenerator:
    def test_empty_selection_returns_empty(self):
        gen = SignalGenerator()
        sel = SelectionResult(date="2026-07-24")
        signals = gen.generate(sel)
        assert signals == []

    def test_generates_buy_for_new_candidates(self):
        gen = SignalGenerator()
        sel = SelectionResult(date="2026-07-24",
                              candidates=[_make_fd("000001", score=0.8, rank=1)])
        signals = gen.generate(sel, current_positions={})
        assert len(signals) == 1
        assert signals[0].action == "buy"
        assert signals[0].code == "000001"

    def test_generates_hold_for_existing_positions(self):
        gen = SignalGenerator()
        sel = SelectionResult(date="2026-07-24",
                              candidates=[_make_fd("000001", score=0.8, rank=1)])
        signals = gen.generate(sel, current_positions={"000001": {"status": "active"}})
        assert len(signals) == 1
        assert signals[0].action == "hold"

    def test_generates_sell_when_out_of_candidates(self):
        gen = SignalGenerator()
        sel = SelectionResult(date="2026-07-24",
                              candidates=[_make_fd("000001", score=0.8, rank=1)])
        signals = gen.generate(sel, current_positions={"000002": {"status": "active"}})
        sell_signals = [s for s in signals if s.action == "sell"]
        assert len(sell_signals) == 1
        assert sell_signals[0].code == "000002"

    def test_generates_buy_and_sell_mixed(self):
        gen = SignalGenerator()
        sel = SelectionResult(date="2026-07-24",
                              candidates=[_make_fd("000001", score=0.8, rank=1)])
        signals = gen.generate(sel, current_positions={"000002": {"status": "active"}})
        actions = {s.action for s in signals}
        assert "buy" in actions
        assert "sell" in actions
        assert len(signals) == 2

    def test_signal_has_required_fields(self):
        gen = SignalGenerator()
        sel = SelectionResult(date="2026-07-24",
                              candidates=[_make_fd("000001", score=0.8, rank=1)])
        signals = gen.generate(sel)
        s = signals[0]
        assert s.code == "000001"
        assert s.date == "2026-07-24"
        assert s.action in ("buy", "sell", "hold")
        assert 0 <= s.strength <= 1
        assert 0 <= s.confidence <= 1
        assert s.signal_id is not None

    def test_strength_scales_with_score(self):
        gen = SignalGenerator()
        sel = SelectionResult(date="2026-07-24",
                              candidates=[_make_fd("000001", score=0.9, rank=1),
                                          _make_fd("000002", score=0.1, rank=2)])
        signals = gen.generate(sel)
        sigs = {s.code: s for s in signals}
        assert sigs["000001"].strength > sigs["000002"].strength

    def test_trade_signal_dataclass_defaults(self):
        s = TradeSignal(code="000001", date="2026-07-24", action="buy")
        assert s.direction == ""
        assert s.magnitude == 0.0
        assert s.ttl == 1
        assert s.signal_id is not None