"""ML 评分器单元测试"""
import pytest
import tempfile
import os
from strategy_layer.ml.linear_scorer import LinearScorer
from strategy_layer.ml.registry import ModelRegistry


def test_linear_scorer_train_predict():
    s = LinearScorer()
    metrics = s.train([[1, 2], [3, 4], [5, 6]], [1, 2, 3])
    assert metrics["r_squared"] > 0.9
    pred = s.predict([[2, 3], [4, 5]])
    assert len(pred) == 2
    assert all(isinstance(p, float) for p in pred)


def test_linear_scorer_save_load():
    s = LinearScorer()
    s.train([[1, 2], [3, 4], [5, 6]], [1, 2, 3])
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "model.json")
        s.save(path)
        s2 = LinearScorer()
        s2.load(path)
        pred1 = s.predict([[2, 3]])
        pred2 = s2.predict([[2, 3]])
        assert abs(pred1[0] - pred2[0]) < 0.001


def test_model_registry():
    with tempfile.TemporaryDirectory() as tmpdir:
        reg = ModelRegistry(registry_dir=tmpdir)
        s = LinearScorer()
        s.train([[1, 2], [3, 4]], [1, 2])
        reg.register("v1", s, {"r_squared": 0.99})
        versions = reg.list_versions()
        assert len(versions) == 1
        s2 = reg.load_latest()
        assert s2 is not None
