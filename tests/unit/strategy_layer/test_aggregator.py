"""Tests for Aggregator"""
from strategy_layer.aggregation.aggregator import Aggregator, FactorDaily, DEFAULT_WEIGHTS, IC_NEGATIVE_FACTORS
from strategy_layer.engine import CleanedFactorMatrix


class TestFactorDaily:
    def test_defaults(self):
        fd = FactorDaily(code="000001", date="2026-07-24", composite_score=0.5)
        assert fd.rank == 0
        assert fd.quantile == 0
        assert fd.ml_score == 0.0
        assert fd.sector == ""

    def test_with_all_fields(self):
        fd = FactorDaily(code="000001", date="2026-07-24", composite_score=0.8,
                        factor_scores={"momentum": 0.5}, rank=1, quantile=5,
                        ml_score=0.3, sector="IT")
        assert fd.factor_scores["momentum"] == 0.5
        assert fd.rank == 1
        assert fd.ml_score == 0.3


class TestAggregator:
    def test_aggregate_sorts_by_score(self):
        agg = Aggregator()
        matrix = CleanedFactorMatrix(date="2026-07-24", data={
            "000001": {"momentum": 0.5, "value": 0.3},
            "000002": {"momentum": 0.9, "value": 0.8},
            "000003": {"momentum": 0.1, "value": 0.2},
        })
        results = agg.aggregate(matrix, weights={"momentum": 1.0, "value": 0.5})
        assert len(results) == 3
        assert results[0].code == "000002"
        assert results[2].code == "000003"

    def test_aggregate_sets_rank_and_quantile(self):
        agg = Aggregator()
        matrix = CleanedFactorMatrix(date="2026-07-24", data={
            f"{i:06d}": {"momentum": 1.0 - i * 0.1} for i in range(10)
        })
        results = agg.aggregate(matrix, weights={"momentum": 1.0})
        assert results[0].rank == 1
        assert results[0].quantile == 1
        assert results[-1].rank == 10
        assert results[-1].quantile == 5

    def test_aggregate_single(self):
        score = Aggregator.aggregate_single({"momentum": 0.5, "value": 0.3},
                                           weights={"momentum": 1.0, "value": 0.5})
        assert score == 0.5 * 1.0 + 0.3 * 0.5

    def test_aggregate_single_with_ml(self):
        score = Aggregator.aggregate_single({"volatility_reversal": 1.0}, ml_score=0.5)
        expected = 0.051 * 1.0 * 0.8 + 0.5 * 0.2
        assert score == expected

    def test_ic_negative_factors_inverted(self):
        agg = Aggregator()
        matrix = CleanedFactorMatrix(date="2026-07-24", data={
            "000001": {"std20": 0.5},
        })
        results = agg.aggregate(matrix, weights={"std20": 1.0})
        assert results[0].composite_score < 0

    def test_get_quantile_distribution(self):
        fds = [FactorDaily(code=f"{i:06d}", date="2026-07-24", composite_score=1.0 - i * 0.1,
                          rank=i + 1, quantile=min(5, i // 2 + 1)) for i in range(10)]
        dist = Aggregator.get_quantile_distribution(fds)
        assert "quantile_counts" in dist
        assert "top_bottom_spread" in dist
        assert dist["top_bottom_spread"] > 0

    def test_empty_quantile_distribution(self):
        dist = Aggregator.get_quantile_distribution([])
        assert dist["top_bottom_spread"] == 0.0

    def test_default_weights_are_defined(self):
        assert len(DEFAULT_WEIGHTS) == 20

    def test_ic_negative_factors_are_defined(self):
        assert len(IC_NEGATIVE_FACTORS) >= 45