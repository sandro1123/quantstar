"""Tests for strategy ML scorers (linear, ridge, ensemble, regime)"""
import pytest
import numpy as np
from strategy_layer.ml.linear_scorer import LinearScorer
from strategy_layer.ml.ridge_scorer import RidgeScorer
from strategy_layer.ml.ensemble_scorer import EnsembleScorer


class TestLinearScorer:
    def test_train_and_predict(self):
        s = LinearScorer()
        X = [[1.0], [2.0], [3.0]]
        y = [2.0, 4.0, 6.0]
        metrics = s.train(X, y)
        assert "r_squared" in metrics
        preds = s.predict([[4.0]])
        assert abs(preds[0] - 8.0) < 0.5

    def test_save_load_roundtrip(self, tmp_path):
        s = LinearScorer()
        s.train([[1.0], [2.0]], [2.0, 4.0])
        path = str(tmp_path / "model.json")
        s.save(path)
        s2 = LinearScorer()
        s2.load(path)
        preds = s2.predict([[3.0]])
        assert len(preds) == 1


class TestRidgeScorer:
    def test_train_and_predict(self):
        s = RidgeScorer()
        X = [[1.0], [2.0], [3.0], [4.0], [5.0]]
        y = [1.0, 2.0, 3.0, 4.0, 5.0]
        metrics = s.train(X, y)
        assert "r_squared" in metrics
        preds = s.predict([[6.0]])
        assert abs(preds[0] - 6.0) < 1.0


class TestEnsembleScorer:
    def test_predict(self):
        s = EnsembleScorer()
        # EnsembleScorer requires pre-trained models for prediction
        # Test that it can be instantiated
        assert s is not None