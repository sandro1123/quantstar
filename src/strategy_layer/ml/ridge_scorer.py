"""Ridge 回归评分器 — 简单线性基线模型"""
import json

import numpy as np

from strategy_layer.ml.base import BaseScorer


class RidgeScorer(BaseScorer):
    def __init__(self):
        self.weights = None
        self.bias = 0.0
        self.alpha = 1.0
        self.feature_names: list[str] = []

    def train(self, X, y, X_val=None, y_val=None):
        X_arr = np.array(X, dtype=np.float64)
        y_arr = np.array(y, dtype=np.float64)
        n_features = X_arr.shape[1]
        X_mean = X_arr.mean(axis=0)
        X_std = X_arr.std(axis=0)
        X_std[X_std == 0] = 1.0
        X_norm = (X_arr - X_mean) / X_std
        ident = np.eye(n_features)
        Xtx = X_norm.T @ X_norm
        Xty = X_norm.T @ y_arr
        result = np.linalg.solve(Xtx + self.alpha * ident, Xty)
        self.weights = result / X_std
        self.bias = float(y_arr.mean() - X_arr.mean(axis=0) @ self.weights)
        self._X_mean = X_mean
        self._X_std = X_std
        y_pred = X_arr @ self.weights + self.bias
        ss_res = np.sum((y_arr - y_pred) ** 2)
        ss_tot = np.sum((y_arr - np.mean(y_arr)) ** 2)
        r_squared = 1 - ss_res / ss_tot if ss_tot > 0 else 0
        return {"r_squared": float(r_squared), "alpha": self.alpha, "n_samples": len(y), "n_features": n_features}

    def predict(self, X):
        if self.weights is None:
            raise ValueError("Model not trained")
        X_arr = np.array(X, dtype=np.float64)
        return (X_arr @ self.weights + self.bias).tolist()

    def save(self, path):
        data = {
            "weights": self.weights.tolist() if self.weights is not None else None,
            "bias": float(self.bias),
            "alpha": self.alpha,
            "feature_names": self.feature_names,
        }
        with open(path, "w") as f:
            json.dump(data, f)

    def load(self, path):
        with open(path) as f:
            data = json.load(f)
        self.weights = np.array(data["weights"]) if data["weights"] else None
        self.bias = data["bias"]
        self.alpha = data.get("alpha", 1.0)
        self.feature_names = data.get("feature_names", [])
