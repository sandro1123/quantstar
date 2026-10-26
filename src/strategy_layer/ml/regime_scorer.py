"""环境感知模型选择器 — 根据市场状态自动切换模型"""
from common.logging import get_logger
from strategy_layer.ml.base import BaseScorer
from strategy_layer.ml.meta_scorer import MetaScorer

logger = get_logger("strategy.ml.regime")


class RegimeAwareScorer(BaseScorer):
    """环境感知评分器 — 牛市/熊市/震荡 自动切换"""

    def __init__(self):
        self._models: dict[str, MetaScorer] = {}
        self._current_regime: str = "neutral"

    @property
    def feature_names(self) -> list[str]:
        scorer = self._models.get(self._current_regime)
        if scorer is None:
            scorer = self._models.get("neutral")
        if scorer is None and self._models:
            scorer = next(iter(self._models.values()))
        if scorer is not None:
            return getattr(scorer, "feature_names", None) or []
        return []

    def train(self, X, y, X_val=None, y_val=None):
        raise NotImplementedError("RegimeAwareScorer is for inference only, train individual scorers separately")

    def add_model(self, regime: str, scorer: MetaScorer):
        self._models[regime] = scorer

    def set_regime(self, regime: str):
        if regime in self._models:
            self._current_regime = regime
            logger.info(f"模型切换: {regime}")

    def predict(self, X):
        scorer = self._models.get(self._current_regime)
        if scorer is None:
            scorer = self._models.get("neutral")
        if scorer is None:
            for s in self._models.values():
                scorer = s
                break
        if scorer is None:
            raise ValueError("No model available for any regime")
        return scorer.predict(X)

    def save(self, path):
        import pickle
        data = {"models": self._models, "current_regime": self._current_regime}
        with open(path, "wb") as f:
            pickle.dump(data, f)

    def load(self, path):
        import pickle
        with open(path, "rb") as f:
            data = pickle.load(f)
        self._models = data["models"]
        self._current_regime = data.get("current_regime", "neutral")


def _is_generic_features(feature_names) -> bool:
    """feature_names 是否为占位特征（f0/f1/...）— 无法映射到真实因子。"""
    if not feature_names:
        return True
    generic = all(f.startswith("f") and f[1:].isdigit() for f in feature_names)
    return generic


def _has_real_features(scorer) -> bool:
    fnames = getattr(scorer, "feature_names", None)
    if not fnames:
        return False
    return not _is_generic_features(fnames)


def build_regime_aware_scorer(registry, versions: list[dict], regime: str) -> RegimeAwareScorer:
    """从模型注册表构建环境感知评分器。优先带真实（非占位）feature_names 的可加载模型。
    跳过 quarantined 模型 (如 v0.8.0-67f-* 疑似泄漏)。"""
    from strategy_layer.ml.registry import ModelRegistry
    registry = registry or ModelRegistry()

    # F3.3: 过滤隔离模型
    versions = [v for v in versions if not v.get("quarantined", False)]

    regime_scorer = RegimeAwareScorer()

    for regime_name in ["bull", "bear", "oscillating", "neutral"]:
        meta = MetaScorer()
        for v in versions:
            v_name = v.get("version", "")
            if regime_name in v_name:
                try:
                    scorer = registry.load(v["version"])
                    if not _has_real_features(scorer):
                        logger.info(f"  skip {v_name}: 无/占位 feature_names")
                        continue
                    val_ic = v.get("metrics", {}).get("val_ic", 0.0)
                    meta.add_scorer(scorer, weight=max(0.0, val_ic))
                except Exception:
                    continue
        if meta.scorers:
            regime_scorer.add_model(regime_name, meta)
            logger.info(f"  环境 {regime_name}: {len(meta.scorers)} 个模型")

    if not regime_scorer._models:
        logger.warning("No regime-specific models found, falling back to best available model")
        try:
            best_model = None
            best_ic = -1.0
            for v in versions:
                try:
                    scorer = registry.load(v["version"])
                except Exception:
                    continue
                if not _has_real_features(scorer):
                    continue
                val_ic = v.get("metrics", {}).get("val_ic", 0.0)
                if val_ic > best_ic:
                    best_ic = val_ic
                    best_model = v
            if best_model:
                scorer = registry.load(best_model["version"])
                meta = MetaScorer()
                meta.add_scorer(scorer, weight=max(0.0, best_ic))
                regime_scorer.add_model(regime, meta)
                logger.info(f"  fallback to {best_model['version']} (val_ic={best_ic:.4f})")
        except Exception as e:
            logger.warning(f"Fallback model loading failed: {e}")

    regime_scorer.set_regime(regime if regime in ["bull", "bear", "oscillating"] else "neutral")
    return regime_scorer


def score_stocks(regime_scorer: RegimeAwareScorer,
                 scores_list: list[dict[str, float]]) -> list[float]:
    """ST-07: 两条 ML 评分路径的统一入口 (run_daily 批量 + get_scores 单股)。

    统一按 scorer.feature_names 顺序对齐特征, 缺失因子填 0.0 — 消除原
    get_scores 用 sorted(keys) 字母序对齐导致的同股不同分 (审计 S-08),
    以及 quarantined 模型只在批量路径被过滤的不一致。
    """
    fnames = regime_scorer.feature_names
    if not fnames or not regime_scorer._models:
        return [0.0] * len(scores_list)
    vectors = [[float(scores.get(f, 0.0) or 0.0) for f in fnames] for scores in scores_list]
    try:
        preds = regime_scorer.predict(vectors)
        return [float(p) for p in preds]
    except Exception as e:
        logger.warning(f"score_stocks predict failed: {e}")
        return [0.0] * len(scores_list)
