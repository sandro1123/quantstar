"""LightGBM 评分器"""
import pickle

import numpy as np

from strategy_layer.ml.base import BaseScorer


class LightGBMScorer(BaseScorer):
    def __init__(self):
        self.model = None
        self.feature_names: list[str] = []

    def train(self, X, y, X_val=None, y_val=None):
        import lightgbm as lgb
        X_arr = np.array(X, dtype=np.float64)
        y_arr = np.array(y, dtype=np.float64)
        model = lgb.LGBMRegressor(
            n_estimators=300, max_depth=4, learning_rate=0.05,
            subsample=0.8, colsample_bytree=0.8,
            reg_alpha=0.1, reg_lambda=1.0,
            min_child_samples=20, random_state=42,
        )
        if X_val is not None and y_val is not None:
            X_val_arr = np.array(X_val, dtype=np.float64)
            y_val_arr = np.array(y_val, dtype=np.float64)
            model.fit(
                X_arr, y_arr,
                eval_set=[(X_val_arr, y_val_arr)],
                callbacks=[lgb.early_stopping(30, verbose=False), lgb.log_evaluation(0)],
            )
        else:
            model.fit(X_arr, y_arr)
        self.model = model
        return {"n_estimators": model.n_estimators_, "max_depth": 4, "n_samples": len(y), "n_features": X_arr.shape[1]}

    def predict(self, X):
        if self.model is None:
            raise ValueError("Model not trained")
        X_arr = np.array(X, dtype=np.float64)
        return self.model.predict(X_arr).tolist()

    def save(self, path):
        with open(path, "wb") as f:
            pickle.dump({"model": self.model, "feature_names": self.feature_names}, f)

    def load(self, path):
        with open(path, "rb") as f:
            data = pickle.load(f)
        if isinstance(data, dict):
            self.model = data["model"]
            self.feature_names = data.get("feature_names", [])
        else:
            self.model = data
            self.feature_names = []
