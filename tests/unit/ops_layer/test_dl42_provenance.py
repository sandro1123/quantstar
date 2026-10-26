"""DL-42: 溯源覆盖率补全 — zombie reaper 收割时填 version/config_hash。

红-绿: 撤回 pipeline_logger.py 补丁时本文件必须失败。
"""
from unittest.mock import patch

import pytest

from common.database import Database
from ops_layer.pipeline_logger import PipelineLogger


@pytest.fixture
def db():
    d = Database(":memory:")
    d.init_db()
    from ops_layer.core.tables import OPS_TABLES_DDL
    conn = d.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.commit()
    return d


def test_zombie_reaper_fills_version_and_config_hash(db):
    """收割僵尸记录时 version/config_hash 必须非空。"""
    pl = PipelineLogger(db)
    conn = db.get_connection()

    # 插入一条过期的 running 记录 (start_time 远早于 now)
    conn.execute(
        "INSERT INTO pipeline_runs (run_id, pipeline_name, date, start_time, status) "
        "VALUES ('test_zombie', 'test_pipeline', '2026-08-20', '2026-08-20 01:00:00', 'running')"
    )
    conn.commit()

    # mock git_head/compute_config_hash 返回固定值 (import 在函数内, patch 源模块)
    with patch("ops_layer.version_tracker.git_head", return_value=("abc1234", "main")), \
         patch("ops_layer.version_tracker.compute_config_hash", return_value="hash123"):
        reaped = pl.reap_expired_runs(timeout_lookup={}, default_timeout_s=300, grace_s=0)

    assert len(reaped) == 1
    row = conn.execute(
        "SELECT status, version, config_hash FROM pipeline_runs WHERE run_id='test_zombie'"
    ).fetchone()
    assert row["status"] == "failed"
    assert row["version"] == "abc1234", "zombie reaper 必须填 version"
    assert row["config_hash"] == "hash123", "zombie reaper 必须填 config_hash"


def test_zombie_reaper_version_fallback_on_error(db):
    """git_head 异常时 version 降级为空串, 不阻断收割。"""
    pl = PipelineLogger(db)
    conn = db.get_connection()

    conn.execute(
        "INSERT INTO pipeline_runs (run_id, pipeline_name, date, start_time, status) "
        "VALUES ('test_zombie2', 'test_pipeline', '2026-08-20', '2026-08-20 01:00:00', 'running')"
    )
    conn.commit()

    with patch("ops_layer.version_tracker.git_head", side_effect=Exception("no git")):
        reaped = pl.reap_expired_runs(timeout_lookup={}, default_timeout_s=300, grace_s=0)

    assert len(reaped) == 1
    row = conn.execute(
        "SELECT status, version FROM pipeline_runs WHERE run_id='test_zombie2'"
    ).fetchone()
    assert row["status"] == "failed"
    # version 降级为空串, 但记录仍被收割 (不阻断)
    assert row["version"] == ""


def test_run_guard_record_run_fills_version(db):
    """run_guard._record_run 写入的记录必须有 version/config_hash。"""
    from ops_layer.run_guard import _record_run, _get_db, _ensure_ops_tables
    import ops_layer.run_guard as rg

    # 注入测试 db
    original_get_db = rg._get_db
    rg._get_db = lambda: db
    try:
        _ensure_ops_tables(db)
        with patch("ops_layer.version_tracker.git_head", return_value=("def5678", "main")), \
             patch("ops_layer.version_tracker.compute_config_hash", return_value="hash456"):
            _record_run("test_script", "success")

        conn = db.get_connection()
        row = conn.execute(
            "SELECT version, config_hash FROM pipeline_runs WHERE pipeline_name='test_script'"
        ).fetchone()
        assert row is not None
        assert row["version"] == "def5678"
        assert row["config_hash"] == "hash456"
    finally:
        rg._get_db = original_get_db
