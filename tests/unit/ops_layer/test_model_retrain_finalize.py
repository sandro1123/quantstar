# -*- coding: utf-8 -*-
"""#94 (2026-09-02): model_retrain no-op 终态缺口 — 三病灶行为测试。

2026-09-01 22:00 实弹 (pipeline.log 20198-20266 + watchdog.jsonl):
model_retrain 双 30s 超时 (compute_fwd_returns/get_training_dataset ghost
线程持锁续跑) → all_rows=None → len(all_rows) TypeError (:2296) →
except 吞掉无 return → 隐式 None → :588 no-op 包裹假 success →
:437 end_run 撞 "database is locked" (busy_timeout=5s 单发) → 异常上抛被
ops.run_pipeline @catch_quantstar_error 吞 → CLI 收 None rc=1 →
行永久 running, monthly_report 依赖饿死 900s。

本文件 TDD RED 先行 — 修复前全部必须红, 修复后必须绿:

病灶① (#94-A): _run_model_retrain except 路径裸奔无 return → 隐式 None,
    违反 :586-587 契约 (数据管道三件显式 HandlerResult)。
    (成功路径尾部同样无 return — 07-19/20 历史"成功"实为 no-op 包裹,
    修复后一并显式化; 该路径需真训练, 由代码评审覆盖, 不在此测。)

病灶② (#94-B): 终态 UPDATE (end_run) 撞锁不得丢终态 (铁律 #12)。
    覆盖 run_pipeline 全部终态分支: success/partial (:439)、failed (:473)、
    timeout (:479)、except (:504)、skipped (:408)。

裁定① (#94 统筹 sm-863edc87771d427d): _run_pipeline_single 僵尸收割从
    「同名过滤」扩「全量收割」— 月度管道 (model_retrain 等) 29 天无人同名
    再触发, 同名收割永远够不着其僵尸行。判定公式 (timeout+宽限) 与
    _ZOMBIE_ERROR_MSG 零改 (pipeline_logger.reap_expired_runs 本体不动)。
"""
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from common.database import Database
from ops_layer.alert_manager import AlertManager
from ops_layer.core.models import HandlerResult, PipelineRunResult
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


def _sync(orch, configs):
    """truthfulness 约定: 单线程 executor + 配置覆盖 + 告警面 mock。"""
    orch._pipeline_configs = configs
    orch.alert_manager.send = MagicMock(return_value=True)
    orch.alert_manager.resolve_pipeline_alerts = MagicMock()
    orch._executor.shutdown(wait=True)
    orch._executor = ThreadPoolExecutor(max_workers=1)
    return orch


# ============ 病灶①: except 路径必须显式 HandlerResult ============

def test_retrain_exception_returns_failed_handlerresult():
    """#94-A: _run_model_retrain 任何内部异常必须显式返回 HandlerResult(success=False)。

    实弹复现: 双 30s 超时 → data_layer 两方法 fallback None →
    len(all_rows) TypeError → except 吞掉无 return → 隐式 None →
    :588 no-op 包裹成假 success。契约 :586-587 不许 except 路径裸奔。
    """
    db, orch = _orch()
    fake_app = MagicMock()
    fake_app.data = MagicMock()
    fake_app.data.compute_fwd_returns.return_value = None   # with_timeout fallback
    fake_app.data.get_training_dataset.return_value = None  # with_timeout fallback

    with patch("bootstrap.bootstrap_app", return_value=fake_app):
        result = orch._run_model_retrain({})

    assert result is not None, \
        "except 路径隐式返回 None — 违反 :586 契约, 会被 no-op 包裹成假 success"
    assert isinstance(result, HandlerResult), \
        f"数据管道必须显式 HandlerResult, 实得 {type(result).__name__}"
    assert result.success is False, "双超时→TypeError 的异常路径必须诚实判 failed"
    assert result.errors, "失败必须带 errors 留痕 (铁律 #12 不静默)"
    db.close()


# ============ 病灶②: finalize 撞锁不得丢终态 ============

class _FlakyLockedEndRun:
    """前 fail_n 次 end_run 撞 'database is locked' (模拟 ghost 线程持写锁),
    之后恢复 — 复现 2026-09-01 22:01:13 :437 撞锁现场。"""

    def __init__(self, real_end, fail_n):
        self.real_end = real_end
        self.fail_n = fail_n
        self.calls = 0

    def __call__(self, run_id, status, error="", version="", config_hash=""):
        self.calls += 1
        if self.calls <= self.fail_n:
            raise sqlite3.OperationalError("database is locked")
        return self.real_end(run_id, status, error, version, config_hash)


def test_success_finalize_locked_keeps_truthful_success():
    """#94-B: handler 真 success 后 end_run 撞锁 (连续两次) →
    终态不得丢, 且必须诚实落 success — 不许被 except 兜成 failed 谎报,
    更不许异常上抛让行永久挂 running (实弹路径)。"""
    db, orch = _orch()
    _sync(orch, {"health_check": {"max_retries": 0, "timeout_s": 5}})
    orch._run_health_check = MagicMock(
        return_value=HandlerResult(success=True, metadata={"rows": 1}))
    flaky = _FlakyLockedEndRun(orch.pipeline_logger.end_run, fail_n=2)
    orch.pipeline_logger.end_run = flaky

    raised = None
    try:
        result = orch.run_pipeline("health_check")
    except Exception as e:  # 修复前: 撞锁异常上抛 = 行永久 running 的实弹路径
        raised = e
        result = None

    assert raised is None, f"end_run 撞锁不得上抛 (行会永久 running): {raised}"
    assert result is not None and result.status == "success", \
        "handler 成功 → 返回值必须诚实 success"
    row = db.get_connection().execute(
        "SELECT status FROM pipeline_runs WHERE pipeline_name='health_check'"
    ).fetchone()
    assert row is not None and row["status"] == "success", \
        f"终态必须诚实落 success (行今={row['status'] if row else '无行'})"
    db.close()


def test_noop_wrap_finalize_locked_keeps_truthful_terminal():
    """#94-B: no-op 包裹 (handler 返 None → :588 假 success) 后 finalize 撞锁
    (一次) → 2026-09-01 实弹正走此路径。终态不得丢; 现行代码会把真跑成功的
    行兜成 failed 谎报, 修复后须诚实 success。"""
    db, orch = _orch()
    _sync(orch, {"health_check": {"max_retries": 0, "timeout_s": 5}})
    orch._run_health_check = MagicMock(return_value=None)  # 走 :588 no-op 包裹
    flaky = _FlakyLockedEndRun(orch.pipeline_logger.end_run, fail_n=1)
    orch.pipeline_logger.end_run = flaky

    try:
        result = orch.run_pipeline("health_check")
    except Exception:
        result = None

    assert result is not None and result.status == "success", \
        "no-op 包裹语义不变 (success), 撞锁重试后必须如实返回"
    row = db.get_connection().execute(
        "SELECT status FROM pipeline_runs WHERE pipeline_name='health_check'"
    ).fetchone()
    assert row is not None and row["status"] == "success", \
        f"no-op 路径终态同样必须落 success, 不许被兜成 failed 谎报 " \
        f"(行今={row['status'] if row else '无行'})"
    db.close()


def test_failed_finalize_locked_still_lands_failed():
    """#94-B: failed 分支 (:473) 的 end_run 同样撞锁不得丢终态
    (统筹裁定: 「须覆盖全部终态分支, 别只裹 success」)。"""
    db, orch = _orch()
    _sync(orch, {"health_check": {"max_retries": 0, "timeout_s": 5}})
    orch._run_health_check = MagicMock(
        return_value=HandlerResult(success=False, errors=["boom"]))
    flaky = _FlakyLockedEndRun(orch.pipeline_logger.end_run, fail_n=2)
    orch.pipeline_logger.end_run = flaky

    try:
        result = orch.run_pipeline("health_check")
    except Exception:
        result = None

    assert result is not None and result.status == "failed", "failed 语义不变"
    row = db.get_connection().execute(
        "SELECT status, error_msg FROM pipeline_runs WHERE pipeline_name='health_check'"
    ).fetchone()
    assert row is not None and row["status"] == "failed", \
        f"failed 终态撞锁同样必须落行 (行今={row['status'] if row else '无行'})"
    assert "boom" in (row["error_msg"] or ""), "error_msg 必须留痕"
    db.close()


def test_timeout_finalize_locked_still_lands_failed():
    """#94-B: timeout 分支 (:479) 的 end_run 撞锁不得丢终态。
    超时产生路径本身已由 test_zombie_reap 收口, 此处只验撞锁韧性。"""
    db, orch = _orch()
    _sync(orch, {"slow_pipe": {"max_retries": 0, "timeout_s": 1}})
    orch._execute_pipeline = lambda name, cfg: time.sleep(3)
    flaky = _FlakyLockedEndRun(orch.pipeline_logger.end_run, fail_n=1)
    orch.pipeline_logger.end_run = flaky

    try:
        result = orch.run_pipeline("slow_pipe")
    except Exception:
        result = None

    assert result is not None and result.status == "failed"
    row = db.get_connection().execute(
        "SELECT status FROM pipeline_runs WHERE pipeline_name='slow_pipe'"
    ).fetchone()
    assert row is not None and row["status"] == "failed", \
        f"timeout 终态撞锁同样必须落行 (行今={row['status'] if row else '无行'})"
    db.close()


# ============ 裁定①: 僵尸收割同名过滤 → 全量收割 ============

def _make_ops():
    from ops_layer import OpsLayer
    db = Database(":memory:")
    db.init_db()
    conn = db.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.commit()
    return db, OpsLayer(db=db, config=None)


def _ago(seconds):
    return (datetime.now() - timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")


def _insert_running(db, name, start_time, run_id):
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO pipeline_runs (run_id, pipeline_name, date, start_time, status) "
        "VALUES (?, ?, ?, ?, 'running')",
        (run_id, name, start_time.split(" ")[0], start_time),
    )
    conn.commit()


def test_unrelated_monthly_zombie_reaped_on_daily_trigger(monkeypatch):
    """裁定① 实弹场景: 月度管道僵尸 (2026-09-01 model_retrain 行挂 running,
    29 天内无人同名再触发) → 触发任意无关日常管道必须顺带收割。"""
    db, ops = _make_ops()
    ops.orchestrator._pipeline_configs = {
        "model_retrain": {"timeout_s": 1800},   # 月度大 timeout (ops.yaml)
        "health_check": {"timeout_s": 30},
    }
    # model_retrain 僵尸: 超 1800+300 宽限 (公式零改)
    _insert_running(db, "model_retrain", _ago(1800 + 300 + 60), "mr_zombie")
    monkeypatch.setattr(
        ops.orchestrator, "run_pipeline",
        lambda name: PipelineRunResult(name=name, status="success"))

    result = ops._run_pipeline_single("health_check")  # 无关日常触发

    assert result.status == "success", "收割不得阻断被触发管道本身"
    row = db.get_connection().execute(
        "SELECT status, error_msg FROM pipeline_runs WHERE run_id='mr_zombie'"
    ).fetchone()
    assert row is not None and row["status"] == "failed", \
        f"月度僵尸必须被全量收割 (行今={row['status'] if row else '无行'}) — " \
        "同名过滤永远够不着 29 天无人触发的月度管道"
    assert "zombie" in (row["error_msg"] or ""), "_ZOMBIE_ERROR_MSG 语义零改"
    db.close()


def test_full_reap_not_touch_fresh_rows_of_other_pipelines(monkeypatch):
    """裁定① 安全边界: 全量收割不得误杀其他管道窗口内 (未过期) 的 running 行
    — 判定公式 (timeout+宽限) 零改的安全面证明。"""
    db, ops = _make_ops()
    ops.orchestrator._pipeline_configs = {
        "model_retrain": {"timeout_s": 1800},
        "health_check": {"timeout_s": 30},
        "kline": {"timeout_s": 600},
    }
    _insert_running(db, "kline", _ago(60), "kline_fresh")            # 窗口内: 60 < 600+300
    _insert_running(db, "model_retrain", _ago(1800 + 300 + 60), "mr_zombie")
    monkeypatch.setattr(
        ops.orchestrator, "run_pipeline",
        lambda name: PipelineRunResult(name=name, status="success"))

    ops._run_pipeline_single("health_check")

    conn = db.get_connection()
    fresh = conn.execute(
        "SELECT status FROM pipeline_runs WHERE run_id='kline_fresh'").fetchone()
    assert fresh["status"] == "running", "窗口内 running 行不得误杀 (公式零改安全面)"
    db.close()
