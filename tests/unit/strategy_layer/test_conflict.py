"""信号冲突检测单元测试"""
import pytest
from strategy_layer.orchestrator.conflict import (
    ConflictDetector,
    ConflictLevel,
    TradeSignal,
    SignalPriority,
    ConflictResolver,
)


def test_soft_conflict():
    det = ConflictDetector()
    signals = [
        TradeSignal(code="000001", action="buy", strength=0.8, source="engine_a", priority=SignalPriority.P1),
        TradeSignal(code="000001", action="hold", strength=0.5, source="engine_b", priority=SignalPriority.P2),
    ]
    reports = det.detect(signals)
    assert len(reports) == 1
    assert reports[0].code == "000001"
    assert reports[0].level == ConflictLevel.SOFT
    assert reports[0].resolution == "keep_buy"


def test_hard_conflict():
    det = ConflictDetector()
    signals = [
        TradeSignal(code="000001", action="buy", strength=0.8, source="engine_a"),
        TradeSignal(code="000001", action="sell", strength=0.9, source="engine_b"),
    ]
    reports = det.detect(signals)
    assert len(reports) == 1
    assert reports[0].code == "000001"
    assert reports[0].level == ConflictLevel.HARD
    assert reports[0].resolution == "downgrade_all"


def test_none_conflict():
    det = ConflictDetector()
    signals = [
        TradeSignal(code="000001", action="buy", strength=0.8, source="engine_a"),
        TradeSignal(code="000002", action="buy", strength=0.6, source="engine_b"),
    ]
    reports = det.detect(signals)
    assert len(reports) == 0


def test_resolver_hard():
    det = ConflictDetector()
    res = ConflictResolver()
    signals = [
        TradeSignal(code="000001", action="buy", strength=0.8, source="engine_a"),
        TradeSignal(code="000001", action="sell", strength=0.9, source="engine_b"),
        TradeSignal(code="000002", action="buy", strength=0.6, source="engine_c"),
    ]
    reports = det.detect(signals)
    resolved = res.resolve(signals, reports)
    buy_sigs = [s for s in resolved if s.code == "000001" and s.action == "buy"]
    sell_sigs = [s for s in resolved if s.code == "000001" and s.action == "sell"]
    assert len(buy_sigs) == 1
    assert len(sell_sigs) == 1
    assert buy_sigs[0].strength == pytest.approx(0.8 * 0.3)
    assert sell_sigs[0].strength == pytest.approx(0.9 * 0.3)
    assert any(s.code == "000002" for s in resolved)


def test_resolver_soft():
    det = ConflictDetector()
    res = ConflictResolver()
    signals = [
        TradeSignal(code="000001", action="buy", strength=0.8, source="engine_a"),
        TradeSignal(code="000001", action="hold", strength=0.5, source="engine_b"),
    ]
    reports = det.detect(signals)
    resolved = res.resolve(signals, reports)
    assert len(resolved) == 1
    assert resolved[0].action == "buy"
    assert resolved[0].strength == 0.8


def test_no_conflict_passthrough():
    det = ConflictDetector()
    res = ConflictResolver()
    signals = [
        TradeSignal(code="000001", action="buy", strength=0.8, source="engine_a"),
        TradeSignal(code="000002", action="sell", strength=0.6, source="engine_b"),
    ]
    reports = det.detect(signals)
    resolved = res.resolve(signals, reports)
    assert len(resolved) == 2