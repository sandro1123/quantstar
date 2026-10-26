# -*- coding: utf-8 -*-
"""W2-8 (2026-09-19): model_retrain NoneType 崩溃守卫 — all_rows=None 不再 TypeError。

病根 (生产实证 alert#86 / pipeline_runs model_retrain_2026-09-02_234653):
  data_layer.get_training_dataset 超时腿返回 None (budget 撞顶静默 None),
  orchestrator.py `_run_model_retrain` 直取 `len(all_rows)` →
  "object of type 'NoneType' has no len()" 穿透 except, 训练未发生,
  告警 86 (critical/escalated) 挂 17 天未消。

修法: len() 前置 None 守卫 — all_rows 为 None 或空 → 显式 failed,
errors 点名病因 (training dataset unavailable/empty), 不再依赖 TypeError
穿透报信。空集与不可得区分 (判例#6): None=腿超时/异常 → "unavailable";
空列表=数据真空 → "empty"。
"""
import sqlite3
from unittest.mock import MagicMock, patch

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
    """bootstrap_app 替身: data 腿只暴露 model_retrain 用到的两个方法。"""
    data = MagicMock()
    data.compute_fwd_returns = MagicMock(return_value=None)
    data.get_training_dataset = MagicMock(return_value=dataset)
    app = MagicMock()
    app.data = data
    return app


def test_retrain_none_dataset_returns_failed_not_typeerror():
    """None (超时腿) → 显式 failed, errors 点名 unavailable, 不得抛 TypeError。"""
    db, orch = _orch()
    with patch("bootstrap.bootstrap_app", return_value=_fake_app(None)):
        r = orch._run_model_retrain({})
    assert r is not None, "handler 不得隐式 None (#94 契约)"
    assert r.success is False
    assert any("unavailable" in e or "None" in e for e in r.errors), r.errors


def test_retrain_empty_dataset_returns_failed_empty():
    """空列表 (数据真空) → 显式 failed, errors 点名 empty, 与 None 可区分。"""
    db, orch = _orch()
    with patch("bootstrap.bootstrap_app", return_value=_fake_app([])):
        r = orch._run_model_retrain({})
    assert r is not None
    assert r.success is False
    assert any("empty" in e for e in r.errors), r.errors


def test_retrain_small_dataset_still_insufficient():
    """<200 行 → 存量 insufficient 语义不变 (守卫不得误伤正常小病)。"""
    db, orch = _orch()
    rows = [("2026-09-01", f"{i:06d}.SH", '{"pe_ttm": 1.0}', 0.01) for i in range(50)]
    with patch("bootstrap.bootstrap_app", return_value=_fake_app(rows)):
        r = orch._run_model_retrain({})
    assert r.success is False
    assert any("insufficient training data" in e for e in r.errors), r.errors
