"""MetaScorer 单元测试"""
import pytest
import tempfile
import os
import numpy as np
from strategy_layer.ml.meta_scorer import MetaScorer
from strategy_layer.ml.base import BaseScorer


class MockScorerA(BaseScorer):
    def __init__(self, value: float):
        self.value = value
        self.feature_names: list[str] = []

    def train(self, X, y):
        return {"n_samples": len(y)}

    def predict(self, X):
        return [self.value] * len(X)

    def save(self, path):
        import pickle
        with open(path, "wb") as f:
            pickle.dump({"value": self.value}, f)

    def load(self, path):
        import pickle
        with open(path, "rb") as f:
            data = pickle.load(f)
        self.value = data["value"]


class MockScorerB(BaseScorer):
    def __init__(self, value: float):
        self.value = value
        self.feature_names: list[str] = []

    def train(self, X, y):
        return {"n_samples": len(y)}

    def predict(self, X):
        return [self.value] * len(X)

    def save(self, path):
        import pickle
        with open(path, "wb") as f:
            pickle.dump({"value": self.value}, f)

    def load(self, path):
        import pickle
        with open(path, "rb") as f:
            data = pickle.load(f)
        self.value = data["value"]


class MockScorerC(BaseScorer):
    def __init__(self, value: float):
        self.value = value
        self.feature_names: list[str] = []

    def train(self, X, y):
        return {"n_samples": len(y)}

    def predict(self, X):
        return [self.value] * len(X)

    def save(self, path):
        import pickle
        with open(path, "wb") as f:
            pickle.dump({"value": self.value}, f)

    def load(self, path):
        import pickle
        with open(path, "rb") as f:
            data = pickle.load(f)
        self.value = data["value"]


class TestMetaScorer:
    def test_empty_meta_scorer_raises(self):
        meta = MetaScorer()
        with pytest.raises(ValueError, match="No scorers in MetaScorer"):
            meta.predict([[1, 2]])

    def test_equal_weight_aggregation(self):
        meta = MetaScorer()
        meta.add_scorer(MockScorerA(0.5))
        meta.add_scorer(MockScorerB(1.0))
        meta.add_scorer(MockScorerC(1.5))
        preds = meta.predict([[1, 2], [3, 4]])
        assert len(preds) == 2
        assert abs(preds[0] - 1.0) < 0.001
        assert abs(preds[1] - 1.0) < 0.001

    def test_ic_weighted_aggregation(self):
        meta = MetaScorer()
        meta.add_scorer(MockScorerA(0.5))
        meta.add_scorer(MockScorerB(1.0))
        meta.add_scorer(MockScorerC(1.5))
        meta.set_ic_weights({
            "MockScorerA": 0.3,
            "MockScorerB": 0.7,
            "MockScorerC": 0.0,
        })
        expected = (0.5 * 0.3 + 1.0 * 0.7 + 1.5 * 0.0) / (0.3 + 0.7 + 0.0)
        preds = meta.predict([[1, 2]])
        assert abs(preds[0] - expected) < 0.001

    def test_ic_weights_ignore_negative(self):
        meta = MetaScorer()
        meta.add_scorer(MockScorerA(0.5))
        meta.add_scorer(MockScorerB(1.0))
        meta.set_ic_weights({"MockScorerA": -0.5, "MockScorerB": 0.5})
        preds = meta.predict([[1, 2]])
        assert abs(preds[0] - 1.0) < 0.001

    def test_save_load(self):
        meta = MetaScorer()
        meta.add_scorer(MockScorerA(0.3))
        meta.add_scorer(MockScorerB(0.7))
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "meta.pkl")
            meta.save(path)
            meta2 = MetaScorer()
            meta2.load(path)
            pred1 = meta.predict([[1, 2]])[0]
            pred2 = meta2.predict([[1, 2]])[0]
            assert abs(pred1 - pred2) < 0.001

    def test_single_scorer(self):
        meta = MetaScorer()
        meta.add_scorer(MockScorerA(0.8))
        preds = meta.predict([[1, 2], [3, 4]])
        assert preds == [0.8, 0.8]

    def test_train_raises(self):
        meta = MetaScorer()
        with pytest.raises(NotImplementedError):
            meta.train([[1, 2]], [1])