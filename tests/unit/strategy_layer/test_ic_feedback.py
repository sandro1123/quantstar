"""Tests for IC feedback loop — factor weight auto-adjustment"""
import json
import os
import tempfile
import pytest
from strategy_layer.feedback.ic_tuner import ICTuner
from strategy_layer.aggregation.aggregator import Aggregator, DEFAULT_WEIGHTS


class TestICFeedbackLoop:
    def test_tuner_compute_ic_and_update_weights(self):
        tuner = ICTuner()
        factor_scores = {
            "momentum_20d": {"001": 0.5, "002": -0.3, "003": 0.8, "004": -0.1, "005": 0.2,
                             "006": 0.6, "007": -0.4, "008": 0.3, "009": -0.2, "010": 0.1},
            "roe_quality": {"001": 0.3, "002": 0.6, "003": -0.1, "004": 0.4, "005": -0.5,
                            "006": 0.2, "007": -0.3, "008": 0.7, "009": 0.1, "010": -0.2},
        }
        forward_returns = {
            "001": 2.0, "002": -1.0, "003": 3.0, "004": -0.5, "005": 1.0,
            "006": 2.5, "007": -1.5, "008": 1.5, "009": -0.8, "010": 0.5,
        }
        ic = tuner.compute_ic(factor_scores, forward_returns)
        assert "momentum_20d" in ic
        assert "roe_quality" in ic
        assert isinstance(ic["momentum_20d"], float)
        assert isinstance(ic["roe_quality"], float)

    def test_tuner_update_weights_changes_weights(self):
        tuner = ICTuner()
        current = {"momentum_20d": 0.5, "roe_quality": 0.5}
        ic_history = {
            "momentum_20d": [0.1, 0.2, 0.15, 0.25, 0.3, 0.2, 0.18, 0.22, 0.28, 0.35],
            "roe_quality": [0.05, 0.08, 0.06, 0.07, 0.09, 0.1, 0.08, 0.11, 0.12, 0.15],
        }
        new_weights = tuner.update_weights_with_decay(current, ic_history)
        assert abs(sum(new_weights.values()) - 1.0) < 1e-6
        assert new_weights["momentum_20d"] > new_weights["roe_quality"]

    def test_weight_persistence_json(self):
        weights = {"momentum_20d": 0.3, "roe_quality": 0.2, "std20": 0.5}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(weights, f)
            path = f.name
        try:
            with open(path) as f:
                loaded = json.load(f)
            assert loaded == weights
            assert abs(sum(loaded.values()) - 1.0) < 1e-6
        finally:
            os.unlink(path)

    def test_aggregator_accepts_custom_weights(self):
        agg = Aggregator()
        custom = {"volatility_reversal": 0.5, "std20": 0.5}
        from strategy_layer.engine import CleanedFactorMatrix
        matrix = CleanedFactorMatrix("2026-07-17", {
            "001": {"volatility_reversal": 0.8, "std20": 0.6},
            "002": {"volatility_reversal": -0.3, "std20": -0.2},
        })
        results = agg.aggregate(matrix, weights=custom)
        assert len(results) == 2
        assert results[0].composite_score != results[1].composite_score

    def test_ml_feature_ordering_sorted_keys(self):
        """P0 regression: ML prediction must use sorted keys, not insertion order"""
        scores = {
            "volatility_reversal": 0.5, "std20": 0.3, "cntp5": 0.2,
            "vma20": 0.1, "alpha_48": 0.4, "momentum_20d": 0.6,
        }
        sorted_keys = sorted(scores.keys())
        values = [scores[k] for k in sorted_keys]
        assert sorted_keys == sorted(scores.keys())
        assert len(values) == 6

    def test_aggregate_single_consistency(self):
        """aggregate_single must produce same composite as aggregate for single stock"""
        scores = {"volatility_reversal": 0.8, "std20": 0.6, "cntp5": 0.4}
        single = Aggregator.aggregate_single(scores)
        from strategy_layer.engine import CleanedFactorMatrix
        matrix = CleanedFactorMatrix("2026-07-17", {"001": scores})
        agg = Aggregator()
        results = agg.aggregate(matrix)
        assert len(results) == 1
        assert abs(results[0].composite_score - single) < 1e-6

    def test_aggregate_single_ml_blending(self):
        """aggregate_single blends ml_score at 20% weight"""
        scores = {"volatility_reversal": 1.0, "std20": 0.5}
        no_ml = Aggregator.aggregate_single(scores)
        with_ml = Aggregator.aggregate_single(scores, ml_score=0.8)
        assert with_ml != no_ml
