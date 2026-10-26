"""跨引擎持仓相关性监控单元测试"""
import pytest
from strategy_layer.orchestrator.correlation import (
    EngineCorrelationMonitor,
    EngineHolding,
)


def test_no_overlap():
    monitor = EngineCorrelationMonitor()
    engines = [
        EngineHolding("engine_a", ["000001", "000002", "000003"]),
        EngineHolding("engine_b", ["600001", "600002"]),
    ]
    report = monitor.analyze(engines)
    assert len(report.pairs) == 1
    assert report.pairs[0].jaccard_similarity == 0.0
    assert report.pairs[0].overlap_count == 0
    assert not report.pairs[0].is_highly_correlated
    assert len(report.highly_correlated_pairs) == 0
    assert len(report.warnings) == 0


def test_partial_overlap():
    monitor = EngineCorrelationMonitor()
    engines = [
        EngineHolding("engine_a", ["000001", "000002", "000003", "000004"]),
        EngineHolding("engine_b", ["000001", "000002", "600001"]),
    ]
    report = monitor.analyze(engines)
    assert len(report.pairs) == 1
    pair = report.pairs[0]
    assert pair.jaccard_similarity == pytest.approx(2 / 5, 0.001)
    assert pair.overlap_count == 2
    assert pair.total_unique == 5
    assert not pair.is_highly_correlated


def test_high_correlation_detection():
    monitor = EngineCorrelationMonitor(threshold=0.5)
    engines = [
        EngineHolding("engine_a", ["000001", "000002", "000003"]),
        EngineHolding("engine_b", ["000001", "000002"]),
    ]
    report = monitor.analyze(engines)
    assert len(report.pairs) == 1
    assert report.pairs[0].is_highly_correlated
    assert len(report.highly_correlated_pairs) == 1
    assert len(report.warnings) == 1
    assert "engine_a" in report.warnings[0]
    assert "engine_b" in report.warnings[0]


def test_three_engines():
    monitor = EngineCorrelationMonitor(threshold=0.3)
    engines = [
        EngineHolding("engine_a", ["000001", "000002"]),
        EngineHolding("engine_b", ["000001", "000003"]),
        EngineHolding("engine_c", ["600001"]),
    ]
    report = monitor.analyze(engines)
    assert len(report.pairs) == 3
    assert len(report.highly_correlated_pairs) == 1  # a-b: 1/3 ≈ 0.33 > 0.3
    assert len(report.warnings) == 1


def test_single_engine():
    monitor = EngineCorrelationMonitor()
    engines = [EngineHolding("engine_a", ["000001", "000002"])]
    report = monitor.analyze(engines)
    assert len(report.pairs) == 0
    assert len(report.warnings) == 0


def test_no_engines():
    monitor = EngineCorrelationMonitor()
    report = monitor.analyze([])
    assert len(report.pairs) == 0
    assert len(report.warnings) == 0


def test_empty_holdings():
    monitor = EngineCorrelationMonitor()
    engines = [
        EngineHolding("engine_a", []),
        EngineHolding("engine_b", ["000001", "000002"]),
    ]
    report = monitor.analyze(engines)
    assert len(report.pairs) == 1
    assert report.pairs[0].jaccard_similarity == 0.0
    assert not report.pairs[0].is_highly_correlated


def test_identical_holdings():
    monitor = EngineCorrelationMonitor()
    engines = [
        EngineHolding("engine_a", ["000001", "000002", "000003"]),
        EngineHolding("engine_b", ["000001", "000002", "000003"]),
    ]
    report = monitor.analyze(engines)
    assert len(report.pairs) == 1
    assert report.pairs[0].jaccard_similarity == 1.0
    assert report.pairs[0].is_highly_correlated
    assert len(report.highly_correlated_pairs) == 1


def test_diversification_score_perfect():
    monitor = EngineCorrelationMonitor()
    engines = [
        EngineHolding("engine_a", ["000001", "000002"]),
        EngineHolding("engine_b", ["600001", "600002"]),
    ]
    score = monitor.get_diversification_score(engines)
    assert score == 1.0


def test_diversification_score_identical():
    monitor = EngineCorrelationMonitor()
    engines = [
        EngineHolding("engine_a", ["000001", "000002"]),
        EngineHolding("engine_b", ["000001", "000002"]),
    ]
    score = monitor.get_diversification_score(engines)
    assert score == 0.0


def test_diversification_score_single():
    monitor = EngineCorrelationMonitor()
    score = monitor.get_diversification_score([EngineHolding("a", ["000001"])])
    assert score == 1.0