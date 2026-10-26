"""ST-07: 两条 ML 评分路径统一 — run_daily 批量与 get_scores 单股同分验收

原缺陷 (S-08): run_daily 按 scorer.feature_names 对齐, get_scores 按 sorted(keys)
字母序 + MetaScorer (且不过滤 quarantined 模型) → 同一只股票两条路径 ml_score 不同。
修复: 统一走 RegimeAwareScorer + score_stocks (feature_names 对齐, 缺失填 0)。
"""
from unittest.mock import MagicMock, patch

from strategy_layer import StrategyLayer
from strategy_layer.ml.regime_scorer import score_stocks


class FakeRegimeScorer:
    """duck-type RegimeAwareScorer: feature_names 顺序敏感, predict=向量求和"""

    def __init__(self, fnames):
        self._fnames = fnames
        self._models = {"neutral": object()}

    @property
    def feature_names(self):
        return self._fnames

    def predict(self, X):
        return [sum(v) for v in X]


SCORES = {"a_factor": 1.0, "b_factor": 2.0, "c_factor": 99.0}
# feature_names 顺序与字母序不同 → 旧实现两条路径向量不同 → 分数不同
FNAMES = ["b_factor", "a_factor"]
EXPECTED = 3.0  # b(2.0) + a(1.0), c 不在 feature_names 内


class TestScoreStocks:
    def test_alignment_by_feature_names(self):
        scorer = FakeRegimeScorer(FNAMES)
        assert score_stocks(scorer, [SCORES]) == [EXPECTED]

    def test_missing_factor_filled_zero(self):
        scorer = FakeRegimeScorer(["b_factor", "missing_factor"])
        assert score_stocks(scorer, [SCORES]) == [2.0]

    def test_no_models_returns_zeros(self):
        scorer = FakeRegimeScorer(FNAMES)
        scorer._models = {}
        assert score_stocks(scorer, [SCORES, SCORES]) == [0.0, 0.0]


class TestTwoPathsConsistent:
    def _layer(self):
        layer = StrategyLayer()
        layer.data_layer = MagicMock()
        layer.data_layer.get_risky_codes.return_value = set()
        return layer

    def _patch_scorer(self, scorer):
        return patch("strategy_layer.ml.regime_scorer.build_regime_aware_scorer",
                     return_value=scorer)

    def test_run_daily_batch_path_ml_score(self):
        layer = self._layer()
        fd = MagicMock()
        fd.code, fd.date = "000001.SZ", "2024-01-02"
        fd.factor_scores = dict(SCORES)
        fd.composite_score, fd.ml_score = 1.0, 0.0
        with patch.object(layer.factor_engine, "compute_all", return_value=MagicMock()):
            with patch.object(layer, "_get_stock_codes", return_value=["000001.SZ"]):
                with patch.object(layer.aggregator, "aggregate", return_value=[fd]):
                    with patch.object(layer.aggregator, "fill_recent_returns"):
                        with patch.object(layer, "market_regime", return_value="neutral"):
                            with patch("strategy_layer.ml.registry.ModelRegistry") as reg_cls:
                                reg_cls.return_value.list_versions.return_value = [{"version": "v1"}]
                                with self._patch_scorer(FakeRegimeScorer(FNAMES)):
                                    with patch.object(layer.selector, "run",
                                                      return_value=MagicMock(candidates=[])):
                                        with patch.object(layer.deep_review, "review_candidates",
                                                          return_value=[]):
                                            with patch.object(layer.signal_generator, "generate",
                                                              return_value=[]):
                                                layer.run_daily("2024-01-02")
        assert fd.ml_score == EXPECTED

    def test_get_scores_single_path_same_ml_score(self):
        layer = self._layer()
        with patch.object(layer.factor_engine, "compute_all",
                          return_value={"000001.SZ": dict(SCORES)}):
            with patch.object(layer, "market_regime", return_value="neutral"):
                with patch("strategy_layer.ml.registry.ModelRegistry") as reg_cls:
                    reg_cls.return_value.list_versions.return_value = [{"version": "v1"}]
                    with self._patch_scorer(FakeRegimeScorer(FNAMES)):
                        with patch("strategy_layer.aggregation.aggregator.Aggregator.aggregate_single",
                                   return_value=0.5) as agg_single:
                            result = layer.get_scores("000001.SZ", "2024-01-02")
        assert result is not None
        # 单股路径传给聚合的 ml_score 必须与批量路径一致
        assert agg_single.call_args.kwargs["ml_score"] == EXPECTED

    def test_both_paths_identical_score(self):
        """验收核心: 同一输入, 两条路径 ml_score 完全一致"""
        layer = self._layer()
        fd = MagicMock()
        fd.code, fd.date = "000001.SZ", "2024-01-02"
        fd.factor_scores = dict(SCORES)
        fd.composite_score, fd.ml_score = 1.0, 0.0
        # 批量路径
        with patch.object(layer.factor_engine, "compute_all", return_value=MagicMock()):
            with patch.object(layer, "_get_stock_codes", return_value=["000001.SZ"]):
                with patch.object(layer.aggregator, "aggregate", return_value=[fd]):
                    with patch.object(layer.aggregator, "fill_recent_returns"):
                        with patch.object(layer, "market_regime", return_value="neutral"):
                            with patch("strategy_layer.ml.registry.ModelRegistry") as reg_cls:
                                reg_cls.return_value.list_versions.return_value = [{"version": "v1"}]
                                with self._patch_scorer(FakeRegimeScorer(FNAMES)):
                                    with patch.object(layer.selector, "run",
                                                      return_value=MagicMock(candidates=[])):
                                        with patch.object(layer.deep_review, "review_candidates",
                                                          return_value=[]):
                                            with patch.object(layer.signal_generator, "generate",
                                                              return_value=[]):
                                                layer.run_daily("2024-01-02")
        batch_ml = fd.ml_score
        # 单股路径
        with patch.object(layer.factor_engine, "compute_all",
                          return_value={"000001.SZ": dict(SCORES)}):
            with patch.object(layer, "market_regime", return_value="neutral"):
                with patch("strategy_layer.ml.registry.ModelRegistry") as reg_cls:
                    reg_cls.return_value.list_versions.return_value = [{"version": "v1"}]
                    with self._patch_scorer(FakeRegimeScorer(FNAMES)):
                        with patch("strategy_layer.aggregation.aggregator.Aggregator.aggregate_single",
                                   return_value=0.5) as agg_single:
                            layer.get_scores("000001.SZ", "2024-01-02")
        single_ml = agg_single.call_args.kwargs["ml_score"]
        assert batch_ml == single_ml == EXPECTED
