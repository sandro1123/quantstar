"""Meta Model — 多模型聚合评分器"""
import numpy as np

from strategy_layer.ml.base import BaseScorer


class MetaScorer(BaseScorer):
    """Meta Model: aggregate multiple scorers by IC-weighted average"""

    def __init__(self):
        self.scorers: list[BaseScorer] = []
        self.weights: list[float] = []
        self.feature_names: list[str] = []

    def add_scorer(self, scorer: BaseScorer, weight: float = 1.0):
        self.scorers.append(scorer)
        self.weights.append(weight)
        if getattr(scorer, "feature_names", None):
            self.feature_names = list(scorer.feature_names)

    def set_ic_weights(self, ic_values: dict[str, float]):
        total = sum(max(0, ic) for ic in ic_values.values())
        if total > 0:
            self.weights = [max(0, ic_values.get(s.__class__.__name__, 0)) / total for s in self.scorers]

    def train(self, X, y):
        raise NotImplementedError("MetaScorer is for inference only, train individual scorers separately")

    def _align(self, X, scorer):
        fnames = getattr(scorer, "feature_names", None)
        if not fnames:
            return X
        if isinstance(X, dict):
            return [X.get(f, 0.0) for f in fnames]
        rows = list(X)
        if rows and isinstance(rows[0], dict):
            return [[row.get(f, 0.0) for f in fnames] for row in rows]
        return X

    def predict(self, X):
        if not self.scorers:
            raise ValueError("No scorers in MetaScorer")
        predictions = [np.array(scorer.predict(self._align(X, scorer)), dtype=np.float64) for scorer in self.scorers]
        if not self.weights:
            self.weights = [1.0 / len(self.scorers)] * len(self.scorers)
        w = np.array(self.weights)
        if len(w) == 1:
            return predictions[0].tolist()
        return np.average(predictions, axis=0, weights=w).tolist()

    def save(self, path):
        import pickle
        data = {"scorers": self.scorers, "weights": self.weights, "feature_names": self.feature_names}
        with open(path, "wb") as f:
            pickle.dump(data, f)

    def load(self, path):
        import pickle
        with open(path, "rb") as f:
            data = pickle.load(f)
        self.scorers = data["scorers"]
        self.weights = data["weights"]
        self.feature_names = data.get("feature_names", [])
