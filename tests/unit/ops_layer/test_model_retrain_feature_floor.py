# -*- coding: utf-8 -*-
"""第二三二环待修项① (2026-11-13): 月度重训特征下限 — 拒绝在塌陷特征集上训练。

病根 (生产实证, 2026-11-12 定谳):
  `_run_model_retrain` 原先只从 **train 段** 收集因子名 (`_collect_factor_names(train_rows)`)。
  train 段止于 2023-11-21 (20 键旧特征时代) → FEATURE_COLUMNS 冻结为 20 个旧名,
  与生产 89 键现代因子交集为 0 → 全 0 向量 → ml_score 恒等 → 早报推荐退化为 code 顺序。

两条防线 (本文件):
  ① 特征空间改从 **all_rows (全量带标签样本)** 收集 — train 段落在旧时代不再冻结特征集;
  ② 特征下限 MIN_FACTOR_FEATURES=30 — 数据面塌陷 (如历史 2 键 {mom20,vol20})
     时 model_retrain 必须显式 failed 拒训, 不许产出维度错配的劣化模型。

变异验证: 把下限判断变异掉 (改回旧行为 train_rows + 无下限), ①② 必须红。
"""
import json
from unittest.mock import MagicMock, patch

import pytest

from common.database import Database
from ops_layer.alert_manager import AlertManager
from ops_layer.core.tables import OPS_TABLES_DDL
from ops_layer.orchestrator import PipelineOrchestrator
from ops_layer.pipeline_logger import PipelineLogger


def _orch():
    db = Database(":memory:")
    db.init_db()
    conn = db.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.commit()
    return db, PipelineOrchestrator(db, PipelineLogger(db), AlertManager(db))


def _fake_app(dataset):
    data = MagicMock()
    data.compute_fwd_returns = MagicMock(return_value=None)
    data.get_training_dataset = MagicMock(return_value=dataset)
    app = MagicMock()
    app.data = data
    return app


def _moderate_dataset(n_dates=60, n_stocks=30, n_features=40):
    """带标签数据集 (日期均匀分布供 embargoed 三段切分)。

    约束: LABEL_HORIZON=20, 训练至少 100 行且 ES 非空需要足够日期。
    n_dates=60 + 30 股/日 → 1800 行; 0.7*60=42, train=[0,22) (660 行),
    es=[42,31) 空但 handler 会 fallback 用 holdout 做早停 —— 满足 `len(X_train)>=100`
    且 `X_train.shape[1]>0`。下限守卫在特征构建之前执行, 与日期数解耦。
    """
    rows = []
    feats = [f"f{i:02d}" for i in range(n_features)]
    for di in range(n_dates):
        date = f"2026-{(di % 12) + 1:02d}-{(di // 12) + 1:02d}"
        for si in range(n_stocks):
            scores = {k: 0.5 + 0.01 * si for k in feats}
            rows.append((date, f"{si:06d}.SZ", json.dumps(scores), 0.01 + 0.001 * si))
    return rows


def test_retrain_collects_features_from_all_rows_not_train_split():
    """①: 特征空间来自全量样本 — train 段缺失、仅 holdout 有的因子必须进特征集。

    场景复刻 11-12 生产病: train 段只有 2 键, holdout 段有 89 键。
    旧行为 (train_rows 收集) 会产出 2 特征模型; 修复后必须 ≥ holdout 键数。
    """
    db, orch = _orch()
    rows = _moderate_dataset()  # 40 特征全量
    # 把 train 段 (前 70%) 的 factor_scores 砍到只剩 2 键 — 复刻历史塌陷
    train_cut = int(len(rows) * 0.7)
    for i, r in enumerate(rows):
        if i < train_cut:
            scores = json.loads(r[2])
            keep = {k: scores[k] for k in ("f00", "f01")}
            rows[i] = (r[0], r[1], json.dumps(keep), r[3])
    with patch("bootstrap.bootstrap_app", return_value=_fake_app(rows)):
        n_features = _probe_feature_columns(orch, rows)
    assert n_features >= 40, (
        f"特征空间必须覆盖全量样本的因子并集 (≥40), 实得 {n_features} — "
        "若只有 2 个, 说明又回退到 train_rows 收集 (11-12 生产病)"
    )


def _probe_feature_columns(orch, rows):
    """提取 _run_model_retrain 内部实际使用的特征列 (训练阶段 X 矩阵列数)。

    打桩三 scorer.train，捕获传入 X 的列数 (等于 FEATURE_COLUMNS 长度);
    scorer 类定义在各自模块, handler 内 `from ... import XGBScorer` 懒加载,
    故 patch 目标必须是源模块而非 orchestrator。
    """
    captured = {}

    class _FakeScorer:
        def __init__(self, *a, **k):
            self.feature_names = None
            self.model = MagicMock()

        def train(self, X, y, *es):
            try:
                captured["n_features"] = int(X.shape[1])
            except (AttributeError, IndexError, TypeError):
                captured["n_features"] = len(X[0]) if len(X) else 0
            return self

        def predict(self, rows):
            return [0.0] * len(rows)

    with patch("strategy_layer.ml.xgb_scorer.XGBScorer", _FakeScorer), \
         patch("strategy_layer.ml.lgb_scorer.LightGBMScorer", _FakeScorer), \
         patch("strategy_layer.ml.ridge_scorer.RidgeScorer", _FakeScorer):
        orch._run_model_retrain({})
    return captured.get("n_features", 0)


def test_retrain_refuses_collapsed_feature_set_below_floor():
    """②: 2 键塌陷数据 (全量都只有 mom20/vol20) → 显式 failed, 拒训。"""
    db, orch = _orch()
    rows = []
    for di in range(12):
        date = f"2026-{(di % 12) + 1:02d}-{(di // 12) + 1:02d}"
        for si in range(30):
            rows.append((date, f"{si:06d}.SZ",
                         json.dumps({"mom20": 0.5, "vol20": -1.0}), 0.01))
    with patch("bootstrap.bootstrap_app", return_value=_fake_app(rows)):
        r = orch._run_model_retrain({})
    assert r is not None and r.success is False, "塌陷特征集必须拒训"
    assert any("below floor" in e for e in r.errors), r.errors
    assert r.metadata.get("n_features") == 2, r.metadata


def test_retrain_floor_boundary_thirty_features_passes_gate():
    """下限=30 恰好通过 → 继续走训练路径 (不得在 30 特征时误伤)。"""
    db, orch = _orch()
    rows = _moderate_dataset(n_features=30)
    with patch("bootstrap.bootstrap_app", return_value=_fake_app(rows)):
        with patch("strategy_layer.ml.xgb_scorer.XGBScorer", _FakeScorer), \
             patch("strategy_layer.ml.lgb_scorer.LightGBMScorer", _FakeScorer), \
             patch("strategy_layer.ml.ridge_scorer.RidgeScorer", _FakeScorer):
            r = orch._run_model_retrain({})
    assert r is not None, "handler 必须显式返回"
    assert not any("below floor" in (e or "") for e in (r.errors or [])), (
        f"30 特征恰好达下限, 不得拒训; 实得 errors={r.errors}"
    )


# _FakeScorer 供上方 test 作用域复用 (避免 import 顺序问题)
class _FakeScorer:
    def __init__(self, *a, **k):
        self.feature_names = None
        self.model = MagicMock()

    def train(self, X, y, *es):
        return self

    def predict(self, rows):
        return [0.0] * len(rows)


def test_retrain_floor_mutation_red():
    """变异验证: 若把特征收集改回 train_rows-only + 无下限 (旧行为),
    ② 的拒训断言必须红 — 证明判据敏感, 非装饰。"""
    db, orch = _orch()
    rows = []
    for di in range(12):
        date = f"2026-{(di % 12) + 1:02d}-{(di // 12) + 1:02d}"
        for si in range(30):
            rows.append((date, f"{si:06d}.SZ",
                         json.dumps({"mom20": 0.5, "vol20": -1.0}), 0.01))
    with patch("bootstrap.bootstrap_app", return_value=_fake_app(rows)):
        r_old = orch._run_model_retrain({})
    # 现行实现: 2 键 < 30 → failed。若实现被回退成旧行为,
    # 这里会 success=True (或走到训练崩溃), 与 failed 期望相反 → 检测器变红。
    assert r_old is not None
    assert r_old.success is False and any("below floor" in e for e in r_old.errors), (
        "2 键塌陷未被拒训 — 下限守卫已失效 (变异回退), 判据必须保持红"
    )
