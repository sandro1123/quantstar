"""集成评分器 — 聚合多个模型"""
import pickle

import numpy as np

from strategy_layer.ml.base import BaseScorer


class EnsembleScorer(BaseScorer):
    def __init__(self):
        self.models: list[BaseScorer] = []
        self.weights: list[float] = []
        self.feature_names: list[str] = []

    def train(self, X, y):
        raise NotImplementedError("Ensemble scorer is for inference only, train individual models separately")

    def predict(self, X):
        if not self.models:
            raise ValueError("No models in ensemble")
        np.array(X, dtype=np.float64)
        predictions = np.array([m.predict(X) for m in self.models])
        if self.weights:
            weights = np.array(self.weights).reshape(-1, 1)
            weighted = np.average(predictions, axis=0, weights=weights)
        else:
            weighted = np.mean(predictions, axis=0)
        return weighted.tolist()

    def save(self, path):
        data = {"models": self.models, "weights": self.weights, "feature_names": self.feature_names}
        with open(path, "wb") as f:
            pickle.dump(data, f)

    def load(self, path):
        with open(path, "rb") as f:
            data = pickle.load(f)
        if isinstance(data, dict):
            if "models" in data and isinstance(data["models"], list):
                self.models = data["models"]
                self.weights = data.get("weights", [])
                self.feature_names = data.get("feature_names", [])
                return
            if "model" in data:
                self.models = [data["model"]]
                self.weights = [1.0]
                return
        raise ValueError(f"Unknown ensemble pickle format: {type(data)}")
