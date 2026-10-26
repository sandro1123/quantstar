"""因子相关性分析单元测试"""
import pytest
import numpy as np
from strategy_layer.factors.correlation import (
    FactorCorrelationAnalyzer,
    CorrelationReport,
    CorrelationPair,
)


class TestCorrelationReport:
    def test_redundancy_ratio_empty(self):
        r = CorrelationReport()
        assert r.redundancy_ratio == 0.0

    def test_redundancy_ratio_no_redundant(self):
        r = CorrelationReport(
            pairs=[CorrelationPair("a", "b", 0.3), CorrelationPair("a", "c", 0.2)],
        )
        assert r.redundancy_ratio == 0.0

    def test_redundancy_ratio_partial(self):
        r = CorrelationReport(
            pairs=[
                CorrelationPair("a", "b", 0.8, is_redundant=True),
                CorrelationPair("a", "c", 0.2),
            ],
            redundant_pairs=[CorrelationPair("a", "b", 0.8, is_redundant=True)],
        )
        assert r.redundancy_ratio == 0.5


class TestFactorCorrelationAnalyzer:
    def test_analyze_less_than_two_factors(self):
        analyzer = FactorCorrelationAnalyzer()
        matrix = {"factor_a": {"000001": 1.0, "000002": 2.0}}
        report = analyzer.analyze(matrix)
        assert len(report.pairs) == 0

    def test_analyze_high_correlation(self):
        analyzer = FactorCorrelationAnalyzer(threshold=0.7)
        codes = [f"{i:06d}" for i in range(100)]
        np.random.seed(42)
        base = np.random.randn(100)
        matrix = {
            "factor_a": {c: float(base[i]) for i, c in enumerate(codes)},
            "factor_b": {c: float(base[i] * 0.5 + np.random.randn() * 0.1) for i, c in enumerate(codes)},
            "factor_c": {c: float(np.random.randn()) for i, c in enumerate(codes)},
        }
        report = analyzer.analyze(matrix)
        assert len(report.pairs) == 3
        for pair in report.pairs:
            if pair.factor_a == "factor_a" and pair.factor_b == "factor_c":
                assert not pair.is_redundant

    def test_analyze_low_correlation(self):
        analyzer = FactorCorrelationAnalyzer(threshold=0.7)
        codes = [f"{i:06d}" for i in range(50)]
        np.random.seed(1)
        matrix = {
            "factor_x": {c: float(np.random.randn()) for c in codes},
            "factor_y": {c: float(np.random.randn()) for c in codes},
        }
        report = analyzer.analyze(matrix)
        assert len(report.pairs) == 1
        assert not report.pairs[0].is_redundant

    def test_analyze_skips_insufficient_data(self):
        analyzer = FactorCorrelationAnalyzer()
        matrix = {
            "factor_a": {"000001": 1.0, "000002": 2.0},
            "factor_b": {"000001": 3.0, "000002": 4.0},
        }
        report = analyzer.analyze(matrix)
        assert len(report.pairs) == 0

    def test_analyze_with_nan_values(self):
        analyzer = FactorCorrelationAnalyzer()
        vals_a = [1, 2, 3, np.nan, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]
        vals_b = [3, 4, 5, 6, np.nan, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17]
        matrix = {
            "factor_a": {f"{i:06d}": float(v) for i, v in enumerate(vals_a)},
            "factor_b": {f"{i:06d}": float(v) for i, v in enumerate(vals_b)},
        }
        report = analyzer.analyze(matrix)
        assert len(report.pairs) == 1
        assert not np.isnan(report.pairs[0].correlation)

    def test_get_weight_penalties_empty(self):
        analyzer = FactorCorrelationAnalyzer()
        report = CorrelationReport()
        assert analyzer.get_weight_penalties(report) == {}

    def test_get_weight_penalties_with_redundant_pairs(self):
        analyzer = FactorCorrelationAnalyzer()
        report = CorrelationReport(
            pairs=[
                CorrelationPair("a", "b", 0.8, is_redundant=True),
                CorrelationPair("a", "c", 0.9, is_redundant=True),
            ],
            redundant_pairs=[
                CorrelationPair("a", "b", 0.8, is_redundant=True),
                CorrelationPair("a", "c", 0.9, is_redundant=True),
            ],
        )
        penalties = analyzer.get_weight_penalties(report)
        assert penalties["a"] == 0.2
        assert penalties["b"] == 0.1
        assert penalties["c"] == 0.1

    def test_spearman_correlation_value(self):
        """Test with known monotonic values to verify Spearman correlation"""
        analyzer = FactorCorrelationAnalyzer(threshold=0.7)
        codes = [f"{i:06d}" for i in range(50)]
        matrix = {
            "perfect_positive": {c: float(i) for i, c in enumerate(codes)},
            "perfect_positive_2": {c: float(i * 2) for i, c in enumerate(codes)},
        }
        report = analyzer.analyze(matrix)
        assert len(report.pairs) == 1
        assert abs(report.pairs[0].correlation - 1.0) < 0.01
        assert report.pairs[0].is_redundant