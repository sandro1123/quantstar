"""模型注册中心 — 管理模型版本"""
import json
import os
import pickle

from common.utils import bj_now
from strategy_layer.ml.base import BaseScorer
from strategy_layer.ml.ensemble_scorer import EnsembleScorer
from strategy_layer.ml.lgb_scorer import LightGBMScorer
from strategy_layer.ml.linear_scorer import LinearScorer
from strategy_layer.ml.xgb_scorer import XGBScorer


class ModelRegistry:
    def __init__(self, config=None, registry_dir: str = "models"):
        self.config = config or {}
        self.registry_dir = self.config.get("strategy.ml.model_dir", registry_dir)
        os.makedirs(registry_dir, exist_ok=True)

    def register(self, version: str, scorer: BaseScorer, metrics: dict) -> str:
        model_dir = os.path.join(self.registry_dir, version)
        os.makedirs(model_dir, exist_ok=True)
        is_pickle = isinstance(scorer, (XGBScorer, LightGBMScorer, EnsembleScorer))
        ext = ".pkl" if is_pickle else ".json"
        model_path = os.path.join(model_dir, f"model{ext}")
        scorer.save(model_path)
        meta = {"version": version, "metrics": metrics, "created_at": bj_now(), "model_type": type(scorer).__name__}
        with open(os.path.join(model_dir, "meta.json"), "w") as f:
            json.dump(meta, f, indent=2)
        return model_path

    def load_latest(self) -> BaseScorer | None:
        """加载最新模型 — 按 meta.json 的 created_at 训练日期排序 (非目录名字母序),
        跳过 quarantined 模型。"""
        metas = self.list_versions()
        # 过滤隔离模型
        metas = [m for m in metas if not m.get("quarantined", False)]
        if not metas:
            return None
        # 按 created_at 降序 (ISO/常见格式可直接字符串比较)
        metas.sort(key=lambda m: m.get("created_at", ""), reverse=True)
        latest_version = metas[0].get("version")
        if not latest_version:
            return None
        return self.load(latest_version)

    def load(self, version: str) -> BaseScorer:
        model_dir = os.path.join(self.registry_dir, version)
        json_path = os.path.join(model_dir, "model.json")
        pkl_path = os.path.join(model_dir, "model.pkl")
        flat_json_path = os.path.join(self.registry_dir, f"{version}.json")
        flat_pkl_path = os.path.join(self.registry_dir, f"{version}.pkl")
        if os.path.exists(pkl_path):
            return self._load_pickle(pkl_path)
        if os.path.exists(flat_pkl_path):
            return self._load_pickle(flat_pkl_path)
        if os.path.exists(json_path):
            meta_path = os.path.join(model_dir, "meta.json")
            model_type = "LinearScorer"
            if os.path.exists(meta_path):
                with open(meta_path) as mf:
                    meta = json.load(mf)
                model_type = meta.get("model_type", "LinearScorer")
            if model_type == "RidgeScorer":
                from strategy_layer.ml.ridge_scorer import RidgeScorer
                scorer = RidgeScorer()
            else:
                scorer = LinearScorer()
            scorer.load(json_path)
            return scorer
        if os.path.exists(flat_json_path):
            scorer = LinearScorer()
            scorer.load(flat_json_path)
            return scorer
        raise FileNotFoundError(f"No model found for version '{version}' at {model_dir}")

    def load_pickle(self, version: str) -> BaseScorer:
        pkl_path = os.path.join(self.registry_dir, version, "model.pkl")
        if not os.path.exists(pkl_path):
            raise FileNotFoundError(f"No pickle model for version '{version}' at {pkl_path}")
        return self._load_pickle(pkl_path)

    def _load_pickle(self, path: str) -> BaseScorer:
        with open(path, "rb") as f:
            data = pickle.load(f)
        if isinstance(data, dict):
            if "models" in data and isinstance(data["models"], list):
                scorer = EnsembleScorer()
                scorer.load(path)
                return scorer
            if "model" in data:
                import lightgbm
                import xgboost
                model = data["model"]
                if isinstance(model, xgboost.XGBModel):
                    scorer = XGBScorer()
                elif isinstance(model, lightgbm.LGBMModel):
                    scorer = LightGBMScorer()
                else:
                    scorer = EnsembleScorer()
                scorer.load(path)
                return scorer
            raise ValueError(f"Unknown pickle dict keys: {list(data.keys())}")
        if hasattr(data, "predict"):
            return data
        raise ValueError(f"Unrecognized pickle object: {type(data)}")

    def load_model_file(self, path: str) -> BaseScorer:
        if path.endswith(".json"):
            scorer = LinearScorer()
            scorer.load(path)
            return scorer
        if path.endswith(".pkl"):
            return self._load_pickle(path)
        raise ValueError(f"Unknown model file format: {path}")

    def list_versions(self) -> list[dict]:
        result = []
        for v in sorted(os.listdir(self.registry_dir)):
            meta_path = os.path.join(self.registry_dir, v, "meta.json")
            if os.path.exists(meta_path):
                with open(meta_path) as f:
                    result.append(json.load(f))
        return result
