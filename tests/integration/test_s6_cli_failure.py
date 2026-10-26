"""F6.2 CLI 异常落库测试 — cli/main.py run_pipeline_cli

覆盖:
- _record_cli_failure: 悬空 running 收口为 failed / 无残留时补 failed 行 / 告警
- run_pipeline_cli: 崩溃 → 落库 + 退出码 1; 正常失败/成功路径不受影响
"""
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import cli.main as cli_main
from common.database import Database
from ops_layer.alert_manager import AlertManager
from ops_layer.core.tables import OPS_TABLES_DDL
from ops_layer.pipeline_logger import PipelineLogger


@pytest.fixture
def db():
    database = Database(":memory:")
    conn = database.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.commit()
    yield database
    database.close()


@pytest.fixture
def guarded(db, monkeypatch):
    monkeypatch.setattr(cli_main, "_get_cli_db", lambda: db)
    monkeypatch.setattr(AlertManager, "push_feishu", lambda self, *a, **k: True)
    return db


def _runs(db):
    return [dict(r) for r in db.get_connection().execute(
        "SELECT pipeline_name, status, error_msg FROM pipeline_runs").fetchall()]


def _alerts(db):
    return [dict(r) for r in db.get_connection().execute(
        "SELECT severity, title FROM alerts").fetchall()]


class TestRecordCliFailure:
    def test_closes_dangling_running_rows(self, guarded):
        pl = PipelineLogger(guarded)
        pl.start_run("morning_brief")  # 模拟 orchestrator 崩溃残留
        cli_main._record_cli_failure("morning_brief", "Traceback: boom")
        rows = _runs(guarded)
        assert len(rows) == 1
        assert rows[0]["status"] == "failed"
        assert "boom" in rows[0]["error_msg"]
        assert rows[0]["pipeline_name"] == "morning_brief"

    def test_inserts_failed_row_when_no_dangling(self, guarded):
        cli_main._record_cli_failure("factor_update", "crash before start_run")
        rows = _runs(guarded)
        assert len(rows) == 1
        assert rows[0]["status"] == "failed"
        assert "crash before start_run" in rows[0]["error_msg"]

    def test_sends_critical_alert(self, guarded):
        cli_main._record_cli_failure("morning_brief", "boom")
        alerts = _alerts(guarded)
        assert len(alerts) == 1
        assert alerts[0]["severity"] == "critical"
        assert "morning_brief" in alerts[0]["title"]

    def test_only_closes_matching_pipeline(self, guarded):
        pl = PipelineLogger(guarded)
        pl.start_run("other_pipeline")
        cli_main._record_cli_failure("morning_brief", "boom")
        rows = {r["pipeline_name"]: r["status"] for r in _runs(guarded)}
        assert rows["other_pipeline"] == "running"  # 不动别人的记录
        assert rows["morning_brief"] == "failed"


class TestRunPipelineCliCrash:
    def test_crash_persists_failure_and_exits_1(self, guarded, monkeypatch):
        def crash(name):
            raise RuntimeError("orchestrator exploded")

        app = SimpleNamespace(ops=SimpleNamespace(run_pipeline=crash))
        monkeypatch.setattr(cli_main, "_lazy_bootstrap", lambda: app)
        result = CliRunner().invoke(cli_main.cli, ["pipeline", "run", "morning_brief"])
        assert result.exit_code == 1
        rows = _runs(guarded)
        assert len(rows) == 1
        assert rows[0]["status"] == "failed"
        assert "orchestrator exploded" in rows[0]["error_msg"]
        # 崩溃告警落库
        alerts = _alerts(guarded)
        assert any("morning_brief" in a["title"] for a in alerts)

    def test_normal_failure_path_unchanged(self, guarded, monkeypatch):
        # 管道正常返回 failed (非崩溃) → 走原路径, 不额外落库 (orchestrator 已记录)
        fake_result = SimpleNamespace(status="failed", error="pipeline reported failure")
        app = SimpleNamespace(ops=SimpleNamespace(run_pipeline=lambda name: fake_result))
        monkeypatch.setattr(cli_main, "_lazy_bootstrap", lambda: app)
        result = CliRunner().invoke(cli_main.cli, ["pipeline", "run", "morning_brief"])
        assert result.exit_code == 1
        assert "失败" in result.output
        assert _runs(guarded) == []  # 正常失败路径不触发崩溃落库

    def test_success_path_unchanged(self, guarded, monkeypatch):
        fake_result = SimpleNamespace(status="success", error="", elapsed_s=1.5)
        app = SimpleNamespace(ops=SimpleNamespace(run_pipeline=lambda name: fake_result))
        monkeypatch.setattr(cli_main, "_lazy_bootstrap", lambda: app)
        result = CliRunner().invoke(cli_main.cli, ["pipeline", "run", "morning_brief"])
        assert result.exit_code == 0
        assert "完成" in result.output
        assert _runs(guarded) == []
