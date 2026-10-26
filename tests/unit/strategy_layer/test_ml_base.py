"""Tests for ML scorer base and CleanedFactorMatrix"""
import pytest
from strategy_layer.engine import CleanedFactorMatrix
from strategy_layer.ml.base import BaseScorer


class TestCleanedFactorMatrix:
    def test_init_and_len(self):
        m = CleanedFactorMatrix(date="2026-07-24", data={"000001": {"mom": 0.5}})
        assert m.date == "2026-07-24"
        assert len(m) == 1

    def test_items(self):
        m = CleanedFactorMatrix(date="2026-07-24", data={"000001": {"mom": 0.5}})
        items = list(m.items())
        assert len(items) == 1
        assert items[0][0] == "000001"

    def test_getitem(self):
        m = CleanedFactorMatrix(date="2026-07-24", data={"000001": {"mom": 0.5}})
        assert m["000001"]["mom"] == 0.5
        assert m["nonexistent"] == {}

    def test_contains(self):
        m = CleanedFactorMatrix(date="2026-07-24", data={"000001": {"mom": 0.5}})
        assert "000001" in m
        assert "nonexistent" not in m

    def test_iter(self):
        m = CleanedFactorMatrix(date="2026-07-24", data={"000001": {"mom": 0.5}, "000002": {"mom": 0.3}})
        codes = list(m)
        assert "000001" in codes
        assert "000002" in codes


class TestBaseScorer:
    def test_cannot_instantiate_abstract(self):
        with pytest.raises(TypeError):
            BaseScorer()

    def test_concrete_scorer(self):
        class TestScorer(BaseScorer):
            def train(self, X, y):
                return {"status": "ok"}
            def predict(self, X):
                return [0.5] * len(X)
            def save(self, path):
                pass
            def load(self, path):
                pass

        scorer = TestScorer()
        result = scorer.train([[1.0], [2.0]], [0.0, 1.0])
        assert result["status"] == "ok"
        preds = scorer.predict([[1.0]])
        assert preds == [0.5]