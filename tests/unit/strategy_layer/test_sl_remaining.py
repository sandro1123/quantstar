"""Tests for strategy layer remaining modules"""
from strategy_layer.feedback.persistence import WeightPersistence
from strategy_layer.lifecycle.canary import CanaryRelease, CanaryConfig, CanaryStatus
from strategy_layer.agent_gate.deep_review import DeepReview
from strategy_layer.aggregation.weight_optimizer import WeightOptimizer


class TestWeightPersistence:
    def test_default_weights_returned_when_no_file(self):
        wp = WeightPersistence(weights_path="/tmp/_nonexistent_weights.json")
        default = {"mom": 0.5, "val": 0.5}
        loaded = wp.load_weights(default)
        assert loaded == default


class TestCanaryRelease:
    def test_canary_register(self):
        c = CanaryRelease()
        status = c.register("v1.0")
        assert status.version == "v1.0"
        assert status.current_traffic >= 0

    def test_canary_evaluate(self):
        c = CanaryRelease()
        c.register("v1.0")
        result = c.evaluate("v1.0", {"sharpe": 0.8, "max_dd": -0.1})
        assert result is not None


class TestDeepReview:
    def test_review_empty_candidates(self):
        dr = DeepReview()
        result = dr.review_candidates([], date="2026-07-24")
        assert result == []


class TestWeightOptimizer:
    def test_optimize_with_ic_history(self):
        wo = WeightOptimizer()
        weights = wo.optimize({"mom": 0.5, "val": 0.3}, [{"factor": "mom", "ic": 0.05}])
        assert len(weights) > 0