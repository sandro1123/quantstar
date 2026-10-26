"""OPS-01: 僵尸 running 记录收割 — 行为边界测试

测"过期判定临界"与"僵尸产生路径的收口", 不复述实现:
- 过期 running → 收为 failed(zombie) 且管道可重新执行 (08-17 事故路径)
- 未过期 running → 不误杀, 仍跳过重复触发
- config 开关关闭 → 不收割
- 超时路径 → 记录收口为 failed(timeout), 不留悬空 running
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock

from common.database import Database
from ops_layer import OpsLayer
from ops_layer.core.models import PipelineRunResult
from ops_layer.core.tables import OPS_TABLES_DDL


def _make_ops(config=None):
    db = Database(":memory:")
    db.init_db()
    conn = db.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.commit()
    return db, OpsLayer(db=db, config=config)


def _insert_running(db, name, start_time, run_id=None):
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO pipeline_runs (run_id, pipeline_name, date, start_time, status) "
        "VALUES (?, ?, ?, ?, 'running')",
        (run_id or f"{name}_zombie", name, start_time.split(" ")[0], start_time),
    )
    conn.commit()


def _ago(seconds):
    return (datetime.now() - timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")


def test_expired_running_reaped_and_pipeline_rerunnable(monkeypatch):
    """08-17 事故路径: 僵尸 running 堵死管道 → 收割后可重新执行。"""
    db, ops = _make_ops()
    ops.orchestrator._pipeline_configs = {"health_check": {"timeout_s": 30}}
    # start_time 早于 timeout(30) + 宽限(300) → 判定僵尸
    _insert_running(db, "health_check", _ago(30 + 300 + 60))

    sentinel = PipelineRunResult(name="health_check", status="success")
    monkeypatch.setattr(ops.orchestrator, "run_pipeline", lambda name: sentinel)

    result = ops._run_pipeline_single("health_check")

    assert result.status == "success"  # 不再被僵尸堵成 skipped
    row = db.get_connection().execute(
        "SELECT status, error_msg FROM pipeline_runs WHERE run_id='health_check_zombie'"
    ).fetchone()
    assert row["status"] == "failed"
    assert "zombie" in row["error_msg"]
    db.close()


def test_fresh_running_not_reaped_still_skipped():
    """临界: 未超过 timeout+宽限 的 running 不误杀, 重复触发仍跳过。"""
    db, ops = _make_ops()
    ops.orchestrator._pipeline_configs = {"health_check": {"timeout_s": 30}}
    _insert_running(db, "health_check", _ago(30 + 300 - 60))  # 窗口内

    result = ops._run_pipeline_single("health_check")

    assert result.status == "skipped"
    assert "already running" in result.error
    row = db.get_connection().execute(
        "SELECT status FROM pipeline_runs WHERE run_id='health_check_zombie'"
    ).fetchone()
    assert row["status"] == "running"  # 未被误杀
    db.close()


def test_reap_disabled_by_config_keeps_zombie():
    """回滚开关: ops.zombie_reap.enabled=False 时不收割 (行为退回旧保护)。"""

    class _Cfg:
        def get(self, key, default=None):
            if key == "ops.zombie_reap.enabled":
                return False
            return default

    db, ops = _make_ops(config=_Cfg())
    ops.orchestrator._pipeline_configs = {"health_check": {"timeout_s": 30}}
    _insert_running(db, "health_check", _ago(30 + 300 + 60))

    result = ops._run_pipeline_single("health_check")

    assert result.status == "skipped"
    row = db.get_connection().execute(
        "SELECT status FROM pipeline_runs WHERE run_id='health_check_zombie'"
    ).fetchone()
    assert row["status"] == "running"
    db.close()


def test_reap_only_touches_expired_records_of_named_pipeline():
    """收割精确性: 只收同名管道的过期记录, 其他管道/未过期记录不动。"""
    db, ops = _make_ops()
    lookup = {"daily_data_pipeline": 1500, "health_check": 30}
    # 过期僵尸 (daily_data_pipeline)
    _insert_running(db, "daily_data_pipeline", _ago(1500 + 300 + 60), run_id="ddp_zombie")
    # 未过期 (daily_data_pipeline)
    _insert_running(db, "daily_data_pipeline", _ago(60), run_id="ddp_fresh")
    # 过期但属于其他管道
    _insert_running(db, "health_check", _ago(9999), run_id="hc_other")

    reaped = ops.pipeline_logger.reap_expired_runs(
        pipeline_name="daily_data_pipeline", timeout_lookup=lookup)

    assert [r["run_id"] for r in reaped] == ["ddp_zombie"]
    conn = db.get_connection()
    assert conn.execute("SELECT status FROM pipeline_runs WHERE run_id='ddp_fresh'").fetchone()["status"] == "running"
    assert conn.execute("SELECT status FROM pipeline_runs WHERE run_id='hc_other'").fetchone()["status"] == "running"
    db.close()


def test_timeout_path_closes_run_and_leaves_no_running_record():
    """超时产生路径: future 超时 → 记录收口 failed(timeout), 不悬空 running。"""
    import time
    from concurrent.futures import ThreadPoolExecutor

    from ops_layer.alert_manager import AlertManager
    from ops_layer.orchestrator import PipelineOrchestrator
    from ops_layer.pipeline_logger import PipelineLogger

    db = Database(":memory:")
    db.init_db()
    conn = db.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.commit()
    orch = PipelineOrchestrator(db, PipelineLogger(db), AlertManager(db))
    orch.alert_manager.send = MagicMock(return_value=True)
    orch._pipeline_configs = {"slow_pipe": {"timeout_s": 1, "max_retries": 0}}
    orch._execute_pipeline = lambda name, cfg: time.sleep(3)

    result = orch.run_pipeline("slow_pipe")

    assert result.status == "failed"
    assert "timeout after 1s" in result.error
    row = conn.execute(
        "SELECT status, error_msg FROM pipeline_runs WHERE pipeline_name='slow_pipe'"
    ).fetchone()
    assert row["status"] == "failed"
    assert "timeout" in row["error_msg"]
    # 无悬空 running (超时路径已收官)
    assert conn.execute(
        "SELECT COUNT(*) FROM pipeline_runs WHERE status='running'"
    ).fetchone()[0] == 0
    orch._executor.shutdown(wait=True)
    db.close()
