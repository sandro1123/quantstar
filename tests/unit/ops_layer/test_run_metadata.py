"""DL-13: pipeline_runs 溯源补全 — version(git short HEAD) + config_hash 指纹

:memory: 库, 无生产写。验证:
- version_tracker 模块级 git_head / compute_config_hash 可用
- orchestrator._get_run_metadata 返回 git short HEAD + 非空 config_hash
- end_run 持久化 version/config_hash 到 pipeline_runs
"""

from common.database import Database
from ops_layer.alert_manager import AlertManager
from ops_layer.core.tables import OPS_TABLES_DDL
from ops_layer.orchestrator import PipelineOrchestrator
from ops_layer.pipeline_logger import PipelineLogger
from ops_layer.version_tracker import compute_config_hash, git_head


def _db():
    db = Database(":memory:")
    conn = db.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.commit()
    return db


def _orchestrator(db):
    return PipelineOrchestrator(db, PipelineLogger(db), AlertManager(db))


def test_git_head_returns_pair():
    commit, branch = git_head()
    assert commit  # worktree 内 git 可用; 不可用时也返回 'unknown' 非空
    assert branch


def test_compute_config_hash_stable_and_nonempty():
    h1 = compute_config_hash()
    h2 = compute_config_hash()
    assert h1 == h2  # 确定性指纹
    assert len(h1) == 16


def test_get_run_metadata_version_and_config_hash():
    db = _db()
    orch = _orchestrator(db)
    version, config_hash = orch._get_run_metadata()
    assert version  # git short HEAD (或 'unknown'), 非空
    assert config_hash  # 不再恒空
    assert len(config_hash) == 16
    db.close()


def test_end_run_persists_version_and_config_hash():
    db = _db()
    pl = PipelineLogger(db)
    run_id = pl.start_run("daily_data_pipeline")
    pl.end_run(run_id, "success", version="abc1234", config_hash="f" * 16)
    row = db.get_connection().execute(
        "SELECT version, config_hash FROM pipeline_runs WHERE run_id=?", (run_id,)
    ).fetchone()
    assert row["version"] == "abc1234"
    assert row["config_hash"] == "f" * 16
    db.close()


def test_run_pipeline_writes_metadata_to_pipeline_runs():
    """run_pipeline 全链路: 新运行记录 version/config_hash 非空 (验收标准)"""
    db = _db()
    orch = _orchestrator(db)
    orch._pipeline_configs = {"probe_pipe": {"max_retries": 0, "timeout_s": 5}}
    # 无 handler → failed, 但 end_run 仍写 version/config_hash
    orch.alert_manager.send = lambda *a, **k: True
    orch.run_pipeline("probe_pipe")
    row = db.get_connection().execute(
        "SELECT version, config_hash FROM pipeline_runs "
        "WHERE pipeline_name='probe_pipe' ORDER BY start_time DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    assert row["version"]
    assert row["config_hash"]
    db.close()
