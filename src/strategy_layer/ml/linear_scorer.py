"""纯 numpy 线性回归评分器 — 零新依赖"""
import json
import os

import numpy as np

from strategy_layer.ml.base import BaseScorer


class LinearScorer(BaseScorer):
    def __init__(self):
        self.weights = None
        self.bias = 0.0
        self.feature_names: list[str] = []

    def train(self, X, y):
        X_arr = np.array(X, dtype=np.float64)
        y_arr = np.array(y, dtype=np.float64)
        X_b = np.column_stack([np.ones(len(X_arr)), X_arr])
        result = np.linalg.lstsq(X_b, y_arr, rcond=None)
        self.weights = result[0][1:]
        self.bias = result[0][0]
        y_pred = X_b @ result[0]
        ss_res = np.sum((y_arr - y_pred) ** 2)
        ss_tot = np.sum((y_arr - np.mean(y_arr)) ** 2)
        r_squared = 1 - ss_res / ss_tot if ss_tot > 0 else 0
        return {"r_squared": float(r_squared), "n_samples": len(y), "n_features": X_arr.shape[1]}

    def predict(self, X):
        if self.weights is None:
            raise ValueError("Model not trained")
        X_arr = np.array(X, dtype=np.float64)
        return (X_arr @ self.weights + self.bias).tolist()

    def save(self, path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        data = {
            "weights": self.weights.tolist() if self.weights is not None else None,
            "bias": float(self.bias),
            "feature_names": self.feature_names,
        }
        with open(path, "w") as f:
            json.dump(data, f)

    def load(self, path):
        with open(path) as f:
            data = json.load(f)
        self.weights = np.array(data["weights"]) if data["weights"] else None
        self.bias = data["bias"]
        self.feature_names = data.get("feature_names", [])
