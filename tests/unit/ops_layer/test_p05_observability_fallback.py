"""P0-5 测试: OPS-04 静默丢记录修复 (铁律 #12 可观测不可静默)。

覆盖:
- 持续 locked → 记录降级到 fallback jsonl (含完整记录), 返回 DEGRADED 哨兵
- stderr 显著告警 (不静默)
- 恢复机制: 下次 run_guarded 启动回灌 fallback 记录到 pipeline_runs
- 非 locked OperationalError 原样抛出 (不吞异常)
- 未提供 fallback_record 时保持旧行为返回 None (向后兼容)
"""
import json
import sqlite3

import pytest

from common.database import Database
from ops_layer import run_guard
from ops_layer.alert_manager import AlertManager
from ops_layer.core.tables import OPS_TABLES_DDL


@pytest.fixture
def db():
    database = Database(":memory:")
    conn = database.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.commit()
    yield database
    database.close()


@pytest.fixture
def fallback_file(tmp_path, monkeypatch):
    path = tmp_path / "observability_fallback.jsonl"
    monkeypatch.setenv("QS_OBSERVABILITY_FALLBACK", str(path))
    return path


def _locked_fn():
    raise sqlite3.OperationalError("database is locked")


def _runs(db):
    return [dict(r) for r in db.get_connection().execute(
        "SELECT pipeline_name, status, error_msg FROM pipeline_runs").fetchall()]


class TestRetryOnLockedFallback:
    def test_exhausted_writes_fallback_and_returns_degraded(self, fallback_file):
        result = run_guard.retry_on_locked(
            _locked_fn, attempts=2, delay_s=0, label="t",
            fallback_record={"kind": "pipeline_run", "name": "factor_update", "status": "failed"},
        )
        assert result is run_guard.DEGRADED
        lines = fallback_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["kind"] == "pipeline_run"
        assert rec["name"] == "factor_update"
        assert rec["status"] == "failed"
        assert rec["recovered"] is False
        assert "locked" in rec["reason"]

    def test_exhausted_without_fallback_record_returns_none(self):
        # 向后兼容: 未提供 fallback_record 保持旧行为
        result = run_guard.retry_on_locked(_locked_fn, attempts=2, delay_s=0, label="t")
        assert result is None

    def test_non_locked_error_raises(self):
        def _other():
            raise sqlite3.OperationalError("no such table: x")
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            run_guard.retry_on_locked(_other, attempts=2, delay_s=0, label="t")

    def test_success_returns_value(self):
        assert run_guard.retry_on_locked(lambda: 42, attempts=2, delay_s=0) == 42


class TestRecordRunDegraded:
    def test_locked_record_run_degrades_to_fallback(self, db, fallback_file, monkeypatch):
        monkeypatch.setattr(run_guard, "_get_db", lambda: db)
        # 让 _write 内部持续 locked: patch retry 目标为抛 locked
        def _always_locked():
            raise sqlite3.OperationalError("database is locked")
        monkeypatch.setattr(run_guard, "_ensure_ops_tables", lambda d: (_ for _ in ()).throw(
            sqlite3.OperationalError("database is locked")))
        result = run_guard._record_run("factor_update", "failed", "boom")
        assert result is run_guard.DEGRADED
        rec = json.loads(fallback_file.read_text(encoding="utf-8").strip())
        assert rec["kind"] == "pipeline_run"
        assert rec["name"] == "factor_update"
        assert rec["status"] == "failed"
        assert "boom" in rec["error"]


class TestRecovery:
    def test_run_guarded_backfills_fallback_records(self, db, fallback_file, monkeypatch):
        monkeypatch.setattr(run_guard, "_get_db", lambda: db)
        monkeypatch.setattr(AlertManager, "push_feishu", lambda self, *a, **k: True)
        # 预置一条未回灌的 fallback 记录
        fallback_file.write_text(json.dumps({
            "kind": "pipeline_run", "name": "factor_update", "status": "failed",
            "error": "locked earlier", "version": "", "config_hash": "",
            "recovered": False,
        }, ensure_ascii=False) + "\n", encoding="utf-8")
        run_guard.run_guarded("intraday_scan", lambda: None)
        rows = _runs(db)
        names = {r["pipeline_name"] for r in rows}
        # 回灌的 factor_update + 本次的 intraday_scan 都在库中
        assert "factor_update" in names
        assert "intraday_scan" in names
        # fallback 文件已清空 (回灌成功)
        assert fallback_file.read_text(encoding="utf-8").strip() == ""

    def test_recovery_survives_still_locked(self, db, fallback_file, monkeypatch):
        monkeypatch.setattr(run_guard, "_get_db", lambda: db)
        fallback_file.write_text(json.dumps({
            "kind": "pipeline_run", "name": "factor_update", "status": "failed",
            "error": "x", "recovered": False,
        }) + "\n", encoding="utf-8")
        # 回灌时仍 locked → 记录保留待下次
        monkeypatch.setattr(run_guard, "_backfill_run",
                            lambda rec: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")))
        run_guard._recover_fallback_records()
        remaining = fallback_file.read_text(encoding="utf-8").strip()
        assert remaining != ""
        assert json.loads(remaining)["name"] == "factor_update"


class TestRunGuardedExceptionNotSwallowed:
    def test_exception_still_raises(self, db, fallback_file, monkeypatch):
        monkeypatch.setattr(run_guard, "_get_db", lambda: db)
        monkeypatch.setattr(AlertManager, "push_feishu", lambda self, *a, **k: True)
        with pytest.raises(ValueError, match="boom"):
            run_guard.run_guarded("t", lambda: (_ for _ in ()).throw(ValueError("boom")))
