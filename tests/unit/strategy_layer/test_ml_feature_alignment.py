"""回归测试：ML 特征对齐 — 修复 ml_score 恒 0。

背景：run_daily 原按 sorted(factor_scores.keys()) 喂模型（88 键），
但模型期待固定 feature_names（68 特征），维度不匹配 → ValueError 被吞 → ml_score=0。
本测试验证 MetaScorer 能将 dict 因子按模型 feature_names 对齐并产出非零预测。
"""
import pytest
import numpy as np

from strategy_layer.ml.meta_scorer import MetaScorer
from strategy_layer.ml.ridge_scorer import RidgeScorer
from strategy_layer.ml.regime_scorer import (
    RegimeAwareScorer,
    build_regime_aware_scorer,
    _is_generic_features,
)


class FeatureRidge(RidgeScorer):
    """带真实 feature_names 的 ridge，用于验证对齐。"""
    def __init__(self, feature_names):
        super().__init__()
        self.feature_names = list(feature_names)
        n = len(feature_names)
        self.weights = np.linspace(0.1, 1.0, n)
        self.bias = 0.5

    def predict(self, X):
        X_arr = np.array(X, dtype=np.float64)
        return (X_arr @ self.weights + self.bias).tolist()


class TestFeatureAlignment:
    def test_is_generic_features(self):
        assert _is_generic_features(["f0", "f1", "f2"]) is True
        assert _is_generic_features(["accrual_quality", "alpha_48"]) is False
        assert _is_generic_features([]) is True

    def test_meta_aligns_dict_by_feature_names(self):
        fnames = ["momentum_20d", "value_discount", "roe_quality"]
        sc = FeatureRidge(fnames)
        meta = MetaScorer()
        meta.add_scorer(sc, weight=1.0)
        # 传入 dict，顺序乱序，应被对齐
        preds = meta.predict([{"roe_quality": 2.0, "momentum_20d": 1.0, "value_discount": 3.0}])
        assert len(preds) == 1
        expected = np.array([1.0, 3.0, 2.0]) @ sc.weights + sc.bias
        assert abs(preds[0] - expected) < 0.001

    def test_missing_feature_defaults_zero(self):
        fnames = ["a", "b", "c"]
        sc = FeatureRidge(fnames)
        meta = MetaScorer()
        meta.add_scorer(sc, weight=1.0)
        preds = meta.predict([{"a": 5.0}])  # b,c 缺失 → 0.0
        assert len(preds) == 1
        expected = np.array([5.0, 0.0, 0.0]) @ sc.weights + sc.bias
        assert abs(preds[0] - expected) < 0.001

    def test_regime_scorer_feature_names_surface(self):
        fnames = ["momentum_20d", "value_discount"]
        sc = FeatureRidge(fnames)
        meta = MetaScorer()
        meta.add_scorer(sc, weight=1.0)
        rs = RegimeAwareScorer()
        rs.add_model("neutral", meta)
        rs.set_regime("neutral")
        assert rs.feature_names == fnames

    def test_regime_scorer_predict_with_dict(self):
        fnames = ["momentum_20d", "value_discount"]
        sc = FeatureRidge(fnames)
        meta = MetaScorer()
        meta.add_scorer(sc, weight=1.0)
        rs = RegimeAwareScorer()
        rs.add_model("neutral", meta)
        rs.set_regime("neutral")
        preds = rs.predict([{"momentum_20d": 1.0, "value_discount": 2.0}])
        assert len(preds) == 1
