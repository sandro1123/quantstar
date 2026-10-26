"""OPS-06: 版本追踪接线 — 行为边界

- record_version 当日去重: 同 commit+hash 同日重复调用只增 1 行
- 不同 commit → 新增行
- run_guarded 记录带 version/config_hash
"""

from unittest.mock import patch

from common.database import Database
from ops_layer.core.tables import OPS_TABLES_DDL
from ops_layer.version_tracker import VersionTracker


def _db():
    db = Database(":memory:")
    db.init_db()
    conn = db.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.commit()
    return db


def test_same_day_same_version_deduped():
    """同 commit+config_hash 当日重复 record → 只增 1 行 (防 cron 刷爆)。"""
    db = _db()
    vt = VersionTracker(db)
    with patch("ops_layer.version_tracker.git_head", return_value=("abc1234", "main")), \
         patch("ops_layer.version_tracker.compute_config_hash", return_value="hash111"):
        vt.record_version()
        vt.record_version()
        vt.record_version()
    n = db.get_connection().execute("SELECT COUNT(*) FROM version_history").fetchone()[0]
    assert n == 1
    db.close()


def test_different_commit_inserts_new_row():
    """commit 变化 → 新行 (版本变更要留痕)。"""
    db = _db()
    vt = VersionTracker(db)
    with patch("ops_layer.version_tracker.git_head", return_value=("abc1234", "main")), \
         patch("ops_layer.version_tracker.compute_config_hash", return_value="hash111"):
        vt.record_version()
    with patch("ops_layer.version_tracker.git_head", return_value=("def5678", "main")), \
         patch("ops_layer.version_tracker.compute_config_hash", return_value="hash111"):
        vt.record_version()
    n = db.get_connection().execute("SELECT COUNT(*) FROM version_history").fetchone()[0]
    assert n == 2
    db.close()


def test_run_guarded_records_version():
    """run_guarded 成功路径 → pipeline_runs 记录带 version/config_hash。"""
    from ops_layer import run_guard

    db = _db()
    with patch.object(run_guard, "_get_db", return_value=db), \
         patch("ops_layer.version_tracker.git_head", return_value=("abc1234", "main")), \
         patch("ops_layer.version_tracker.compute_config_hash", return_value="hash111"):
        run_guard.run_guarded("test_script", lambda: 0)

    row = db.get_connection().execute(
        "SELECT version, config_hash, status FROM pipeline_runs WHERE pipeline_name='test_script'"
    ).fetchone()
    assert row["status"] == "success"
    assert row["version"] == "abc1234"
    assert row["config_hash"] == "hash111"
    db.close()
