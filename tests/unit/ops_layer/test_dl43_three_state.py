"""DL-43 验收测试 — 三态 (success/partial/failed) 可区分 + partial 告警

核心病灶: partial 曾被 HandlerResult.success=True 折叠成 pipeline_runs.status='success'
静默入库 (DL-06 假成功: lhb_daily 报 success written=61 但表无数据)。
本测试保证: partial 独立落库 + 发 warning 告警 + 不阻塞下游 + 不混入 success_rate。
"""
import sqlite3
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from common.database import Database
from data_layer.pipelines.base import PipelineResult
from ops_layer.alert_manager import AlertManager
from ops_layer.core.models import HandlerResult
from ops_layer.orchestrator import PipelineOrchestrator
from ops_layer.pipeline_logger import PipelineLogger


# ---------------------------------------------------------------------------
# HandlerResult.final_status 三态映射
# ---------------------------------------------------------------------------

def test_final_status_success():
    assert HandlerResult(success=True).final_status == "success"


def test_final_status_failed():
    assert HandlerResult(success=False, errors=["x"]).final_status == "failed"


def test_final_status_partial():
    r = HandlerResult(success=True, metadata={"partial": True, "errors": ["degraded"]})
    assert r.final_status == "partial"


def test_final_status_failed_wins_over_partial():
    # success=False 优先判 failed, 即使 metadata 标了 partial
    r = HandlerResult(success=False, metadata={"partial": True})
    assert r.final_status == "failed"


# ---------------------------------------------------------------------------
# orchestrator: partial 落库 + 告警
# ---------------------------------------------------------------------------

def _orchestrator():
    db = Database(":memory:")
    db.init_db()
    from ops_layer.core.tables import OPS_TABLES_DDL

    conn = db.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.commit()
    return db, PipelineOrchestrator(db, PipelineLogger(db), AlertManager(db))


def _run_with_handler(db, orchestrator, handler_result):
    """让 daily_data_pipeline 的 handler 返回指定 HandlerResult, 跑一次 run_pipeline。"""
    orchestrator._pipeline_configs = {
        "daily_data_pipeline": {"max_retries": 0, "timeout_s": 2}
    }
    orchestrator.alert_manager.send = MagicMock(return_value=True)
    orchestrator._execute_pipeline = MagicMock(return_value=handler_result)
    orchestrator._executor.shutdown(wait=True)
    orchestrator._executor = ThreadPoolExecutor(max_workers=1)
    return orchestrator.run_pipeline("daily_data_pipeline")


def test_partial_recorded_as_partial_not_success():
    """partial handler → pipeline_runs.status='partial' (不再是 'success')"""
    db, orch = _orchestrator()
    hr = HandlerResult(success=True, records_written=100,
                       metadata={"partial": True, "errors": ["kline: 2 symbols empty"]})
    result = _run_with_handler(db, orch, hr)
    assert result.status == "partial"
    row = db.get_connection().execute(
        "SELECT status, error_msg FROM pipeline_runs WHERE pipeline_name='daily_data_pipeline'"
    ).fetchone()
    assert row["status"] == "partial", f"partial 不得记为 {row['status']}"
    assert "kline" in row["error_msg"]
    db.close()


def test_partial_sends_warning_alert():
    """partial 必须发 warning 告警 (铁律 #12 不可静默)"""
    db, orch = _orchestrator()
    hr = HandlerResult(success=True, metadata={"partial": True, "errors": ["degraded"]})
    _run_with_handler(db, orch, hr)
    orch.alert_manager.send.assert_called()
    alert = orch.alert_manager.send.call_args[0][0]
    assert alert.severity == "warning"
    assert "partial" in alert.title.lower() or "部分成功" in alert.title
    db.close()


def test_success_does_not_send_partial_alert():
    """success 不发 partial 告警, 且 resolve 既有告警"""
    db, orch = _orchestrator()
    hr = HandlerResult(success=True, records_written=100)
    result = _run_with_handler(db, orch, hr)
    assert result.status == "success"
    # send 不应被以 partial 告警调用
    for call in orch.alert_manager.send.call_args_list:
        alert = call[0][0]
        assert "部分成功" not in alert.title
    db.close()


def test_partial_resets_consecutive_failures():
    """partial 数据已落库 → 重置连续失败计数 (熔断器只针对硬失败)"""
    db, orch = _orchestrator()
    orch._consecutive_failures["daily_data_pipeline"] = 2
    hr = HandlerResult(success=True, metadata={"partial": True})
    _run_with_handler(db, orch, hr)
    assert orch._consecutive_failures["daily_data_pipeline"] == 0
    db.close()


# ---------------------------------------------------------------------------
# 依赖检查: partial 不阻塞下游
# ---------------------------------------------------------------------------

def test_partial_dependency_does_not_block_downstream():
    """上游 partial (数据已落库) → 下游可放行"""
    db, orch = _orchestrator()
    conn = db.get_connection()
    from common.utils import bj_date
    conn.execute(
        "INSERT INTO pipeline_runs (run_id, pipeline_name, date, start_time, status) "
        "VALUES ('r1', 'daily_data_pipeline', ?, '08:00:00', 'partial')",
        (bj_date(),))
    conn.commit()
    orch._pipeline_configs = {
        "factor_update": {"depends_on": ["daily_data_pipeline"]}
    }
    can_run, reason = orch.can_run("factor_update")
    assert can_run, f"partial 依赖应放行, 实际: {reason}"
    db.close()


def test_failed_dependency_still_blocks():
    """failed 依赖仍阻塞 (回归保护: 三态改动不放松 failed 语义)"""
    db, orch = _orchestrator()
    conn = db.get_connection()
    from common.utils import bj_date
    conn.execute(
        "INSERT INTO pipeline_runs (run_id, pipeline_name, date, start_time, status) "
        "VALUES ('r1', 'daily_data_pipeline', ?, '08:00:00', 'failed')",
        (bj_date(),))
    conn.commit()
    orch._pipeline_configs = {
        "factor_update": {"depends_on": ["daily_data_pipeline"]}
    }
    # daily_data_pipeline failed 且无数据 → 阻塞
    can_run, reason = orch.can_run("factor_update")
    assert not can_run
    db.close()


# ---------------------------------------------------------------------------
# schema: CHECK 约束容纳 partial
# ---------------------------------------------------------------------------

def test_schema_check_allows_partial():
    db, _ = _orchestrator()
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO pipeline_runs (run_id, pipeline_name, date, start_time, status) "
        "VALUES ('probe', 'p', '2026-01-01', '00:00:00', 'partial')")
    conn.commit()
    row = conn.execute(
        "SELECT status FROM pipeline_runs WHERE run_id='probe'").fetchone()
    assert row["status"] == "partial"
    db.close()


def test_schema_check_rejects_invalid_status():
    db, _ = _orchestrator()
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO pipeline_runs (run_id, pipeline_name, date, start_time, status) "
            "VALUES ('probe', 'p', '2026-01-01', '00:00:00', 'bogus')")
        conn.commit()
        assert False, "bogus status 应被 CHECK 拒绝"
    except sqlite3.IntegrityError:
        pass
    db.close()


# ---------------------------------------------------------------------------
# 迁移脚本: 旧 schema (无 partial) → 新 schema
# ---------------------------------------------------------------------------

def test_migration_script_dry_run_and_apply(tmp_path):
    """旧库 (CHECK 无 partial) → 迁移后可写 partial, 历史行不变"""
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE pipeline_runs (
            run_id TEXT PRIMARY KEY, pipeline_name TEXT NOT NULL,
            date TEXT NOT NULL, start_time TEXT NOT NULL, end_time TEXT,
            status TEXT NOT NULL CHECK (status IN ('running','success','failed','skipped')),
            error_msg TEXT, duration_s REAL, version TEXT, config_hash TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime')))
    """)
    conn.execute(
        "INSERT INTO pipeline_runs (run_id, pipeline_name, date, start_time, status) "
        "VALUES ('h1', 'kline', '2026-08-20', '15:00:00', 'success')")
    conn.commit()
    conn.close()

    script = Path(__file__).resolve().parents[3] / "scripts" / "migrate_pipeline_runs_partial.py"

    # dry-run 不改库
    r = subprocess.run(
        [sys.executable, str(script), "--db", str(db_path), "--dry-run"],
        capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    conn = sqlite3.connect(str(db_path))
    assert "'partial'" not in conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='pipeline_runs'").fetchone()[0]
    conn.close()

    # 正式迁移
    r = subprocess.run(
        [sys.executable, str(script), "--db", str(db_path),
         "--evidence", str(tmp_path / "ev.jsonl")],
        capture_output=True, text=True)
    assert r.returncode == 0, r.stderr

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='pipeline_runs'").fetchone()[0]
    assert "'partial'" in ddl
    # 历史行保留
    row = conn.execute(
        "SELECT status FROM pipeline_runs WHERE run_id='h1'").fetchone()
    assert row["status"] == "success"
    # partial 可写
    conn.execute(
        "INSERT INTO pipeline_runs (run_id, pipeline_name, date, start_time, status) "
        "VALUES ('p1', 'kline', '2026-08-22', '15:00:00', 'partial')")
    conn.commit()
    conn.close()


def test_migration_idempotent(tmp_path):
    """已迁移的库再跑一次 → 跳过, 不报错"""
    db_path = tmp_path / "new.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE pipeline_runs (
            run_id TEXT PRIMARY KEY, pipeline_name TEXT NOT NULL,
            date TEXT NOT NULL, start_time TEXT NOT NULL, end_time TEXT,
            status TEXT NOT NULL CHECK (status IN ('running','success','partial','failed','skipped')),
            error_msg TEXT, duration_s REAL, version TEXT, config_hash TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime')))
    """)
    conn.commit()
    conn.close()

    script = Path(__file__).resolve().parents[3] / "scripts" / "migrate_pipeline_runs_partial.py"
    r = subprocess.run(
        [sys.executable, str(script), "--db", str(db_path)],
        capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "无需迁移" in r.stdout


def test_migration_leaves_no_residue_and_records_sample_evidence(tmp_path):
    """迁移后不得残留 pipeline_runs_new; 证据须含抽样对照计数"""
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE pipeline_runs (
            run_id TEXT PRIMARY KEY, pipeline_name TEXT NOT NULL,
            date TEXT NOT NULL, start_time TEXT NOT NULL, end_time TEXT,
            status TEXT NOT NULL CHECK (status IN ('running','success','failed','skipped')),
            error_msg TEXT, duration_s REAL, version TEXT, config_hash TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime')))
    """)
    for i in range(10):
        conn.execute(
            "INSERT INTO pipeline_runs (run_id, pipeline_name, date, start_time, status) "
            "VALUES (?, 'kline', '2026-08-20', '15:00:00', 'success')", (f"h{i}",))
    conn.commit()
    conn.close()

    script = Path(__file__).resolve().parents[3] / "scripts" / "migrate_pipeline_runs_partial.py"
    ev_path = tmp_path / "ev.jsonl"
    r = subprocess.run(
        [sys.executable, str(script), "--db", str(db_path), "--evidence", str(ev_path)],
        capture_output=True, text=True)
    assert r.returncode == 0, r.stderr

    conn = sqlite3.connect(str(db_path))
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='pipeline_runs_new'"
    ).fetchone() is None, "迁移后不得残留 pipeline_runs_new"
    assert conn.execute("SELECT COUNT(*) FROM pipeline_runs").fetchone()[0] == 10
    conn.close()

    evidence = json.loads(ev_path.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert evidence["sample_checked"] > 0
    assert evidence["sample_mismatch"] == 0
    assert evidence["partial_writable"] is True
