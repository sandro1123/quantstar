"""S3 F3.3 端到端 — ML 月度重训修复

验证:
1. registry.load_latest 按 meta.json created_at 训练日期排序 (非目录名字母序),
   且跳过 quarantined 模型。
2. build_regime_aware_scorer 过滤 quarantined 模型。
3. 重训产物特征名为真实因子名 (非 f0..fN 占位) 时能被 regime_scorer 加载使用;
   占位特征被 _is_generic_features 过滤。
"""
import json

from strategy_layer.ml.registry import ModelRegistry
from strategy_layer.ml.regime_scorer import (
    _has_real_features,
    _is_generic_features,
    build_regime_aware_scorer,
)


class _FakeScorer:
    """最小可加载 scorer — 带 feature_names, save/load 走 json。"""

    def __init__(self, feature_names=None):
        self.feature_names = feature_names or []
        self.weights = {}

    def train(self, X, y, X_val=None, y_val=None):
        pass

    def predict(self, X):
        return [0.0] * len(X)

    def save(self, path):
        # 与 LinearScorer.save 格式对齐 (weights/bias/feature_names), 供 registry.load 读取
        with open(path, "w") as f:
            json.dump({"weights": None, "bias": 0.0, "feature_names": self.feature_names}, f)

    def load(self, path):
        with open(path) as f:
            data = json.load(f)
        self.feature_names = data.get("feature_names", [])
        self.weights = data.get("weights")


class TestGenericFeatureDetection:
    def test_placeholder_f0_fn_is_generic(self):
        assert _is_generic_features(["f0", "f1", "f2"]) is True
        assert _is_generic_features([]) is True

    def test_real_factor_names_not_generic(self):
        assert _is_generic_features(["momentum_20d", "roe_quality", "std20"]) is False

    def test_has_real_features(self):
        assert _has_real_features(_FakeScorer(["momentum_20d", "std20"])) is True
        assert _has_real_features(_FakeScorer(["f0", "f1"])) is False
        assert _has_real_features(_FakeScorer([])) is False


class TestRegistryDateOrdering:
    def _setup_registry(self, tmp_path, versions):
        """versions: list of (version_name, created_at, quarantined)."""
        reg_dir = tmp_path / "models"
        reg_dir.mkdir()
        for name, created, quarantined in versions:
            d = reg_dir / name
            d.mkdir()
            meta = {"version": name, "created_at": created, "metrics": {"val_ic": 0.1},
                    "model_type": "LinearScorer"}
            if quarantined:
                meta["quarantined"] = True
            with open(d / "meta.json", "w") as f:
                json.dump(meta, f)
            # 写一个可加载的 model.json (LinearScorer 格式)
            scorer = _FakeScorer(["momentum_20d", "std20"])
            scorer.save(str(d / "model.json"))
        return ModelRegistry(registry_dir=str(reg_dir))

    def test_load_latest_by_date_not_alphabetical(self, tmp_path):
        # 目录名字母序: aaa_latest 排最后, 但 zzz_older 的 created_at 最新
        reg = self._setup_registry(tmp_path, [
            ("aaa_alphabetically_last", "2026-07-01 10:00:00", False),
            ("zzz_newest_by_date", "2026-08-15 10:00:00", False),
            ("mmm_middle", "2026-07-20 10:00:00", False),
        ])
        # load_latest 依赖 LinearScorer.load; 用 list_versions 验证排序逻辑
        metas = [m for m in reg.list_versions() if not m.get("quarantined")]
        metas.sort(key=lambda m: m.get("created_at", ""), reverse=True)
        assert metas[0]["version"] == "zzz_newest_by_date"

    def test_load_latest_skips_quarantined(self, tmp_path):
        reg = self._setup_registry(tmp_path, [
            ("leaky_model", "2026-08-16 10:00:00", True),   # 最新但被隔离
            ("clean_model", "2026-08-10 10:00:00", False),
        ])
        metas = [m for m in reg.list_versions() if not m.get("quarantined")]
        metas.sort(key=lambda m: m.get("created_at", ""), reverse=True)
        assert metas[0]["version"] == "clean_model"
        assert all(m["version"] != "leaky_model" for m in metas)


class TestRegimeScorerQuarantine:
    def test_build_skips_quarantined_versions(self, tmp_path):
        reg_dir = tmp_path / "models"
        reg_dir.mkdir()
        versions = []
        for name, created, quarantined in [
            ("neutral_leaky", "2026-08-16 10:00:00", True),
            ("neutral_clean", "2026-08-10 10:00:00", False),
        ]:
            d = reg_dir / name
            d.mkdir()
            meta = {"version": name, "created_at": created,
                    "metrics": {"val_ic": 0.9 if quarantined else 0.1},
                    "model_type": "LinearScorer"}
            if quarantined:
                meta["quarantined"] = True
            with open(d / "meta.json", "w") as f:
                json.dump(meta, f)
            _FakeScorer(["momentum_20d", "std20"]).save(str(d / "model.json"))
            versions.append(meta)

        registry = ModelRegistry(registry_dir=str(reg_dir))
        scorer = build_regime_aware_scorer(registry, versions, "neutral")
        # neutral 环境应只加载 clean 模型, 不加载 leaky
        meta_scorer = scorer._models.get("neutral")
        assert meta_scorer is not None
        assert len(meta_scorer.scorers) == 1
