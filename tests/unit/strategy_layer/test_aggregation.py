"""聚合单元测试"""
import pytest
from strategy_layer.aggregation.aggregator import Aggregator, FactorDaily
from strategy_layer.engine import CleanedFactorMatrix


def test_aggregation_basic():
    agg = Aggregator()
    matrix = CleanedFactorMatrix(date="2026-07-17", data={
        "000001": {"momentum_20d": 1.5, "value_discount": -0.5, "roe_quality": 0.8},
        "000002": {"momentum_20d": -0.5, "value_discount": 1.0, "roe_quality": -0.3},
    })
    results = agg.aggregate(matrix)
    assert len(results) == 2
    assert results[0].rank == 1
    assert results[0].quantile >= 1
    assert results[0].code in ("000001", "000002")


def test_aggregation_empty():
    agg = Aggregator()
    matrix = CleanedFactorMatrix(date="2026-07-17", data={})
    results = agg.aggregate(matrix)
    assert len(results) == 0


def test_aggregation_ranking():
    agg = Aggregator()
    matrix = CleanedFactorMatrix(date="2026-07-17", data={
        "A": {"momentum_20d": 2.0},
        "B": {"momentum_20d": 1.0},
        "C": {"momentum_20d": 0.0},
    })
    results = agg.aggregate(matrix, weights={"momentum_20d": 1.0})
    assert results[0].code == "A"
    assert results[0].rank == 1
    assert results[-1].code == "C"


def test_quantile_assignment():
    agg = Aggregator()
    codes = {f"S{i:04d}": {"momentum_20d": float(i)} for i in range(1, 11)}
    matrix = CleanedFactorMatrix(date="2026-07-17", data=codes)
    results = agg.aggregate(matrix, weights={"momentum_20d": 1.0})
    assert len(results) == 10
    assert results[0].quantile == 1
    assert results[-1].quantile == 5
    for r in results:
        assert 1 <= r.quantile <= 5


def test_quantile_distribution():
    agg = Aggregator()
    codes = {f"S{i:04d}": {"momentum_20d": float(i)} for i in range(1, 11)}
    matrix = CleanedFactorMatrix(date="2026-07-17", data=codes)
    results = agg.aggregate(matrix, weights={"momentum_20d": 1.0})
    dist = Aggregator.get_quantile_distribution(results)
    assert sum(dist["quantile_counts"].values()) == 10
    assert dist["mean_scores"][1] > dist["mean_scores"][5]
    assert dist["top_bottom_spread"] > 0


def test_quantile_distribution_empty():
    dist = Aggregator.get_quantile_distribution([])
    assert dist["quantile_counts"] == {}
    assert dist["top_bottom_spread"] == 0.0


def test_top_bottom_spread():
    agg = Aggregator()
    codes = {f"S{i:04d}": {"momentum_20d": float(i)} for i in range(1, 11)}
    matrix = CleanedFactorMatrix(date="2026-07-17", data=codes)
    results = agg.aggregate(matrix, weights={"momentum_20d": 1.0})
    dist = Aggregator.get_quantile_distribution(results)
    q1 = [r.composite_score for r in results if r.quantile == 1]
    q5 = [r.composite_score for r in results if r.quantile == 5]
    expected_spread = sum(q1) / len(q1) - sum(q5) / len(q5)
    assert dist["top_bottom_spread"] == pytest.approx(expected_spread, rel=1e-4)
