"""F6.1 直连脚本可观测哨兵测试 — ops_layer/run_guard.py

覆盖:
- 成功 (None/0) → pipeline_runs success
- 异常 → failed + traceback + critical 告警 + 重新抛出
- 非零退出码 → failed (+ warning 告警, alert_on_exit_code 可关)
- 可观测链路自身失败不掩盖脚本原始结果
"""
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
def guarded(db, monkeypatch):
    """哨兵指向内存库 + 屏蔽真实飞书推送"""
    monkeypatch.setattr(run_guard, "_get_db", lambda: db)
    monkeypatch.setattr(AlertManager, "push_feishu", lambda self, *a, **k: True)
    return db


def _runs(db):
    return [dict(r) for r in db.get_connection().execute(
        "SELECT pipeline_name, status, error_msg FROM pipeline_runs").fetchall()]


def _alerts(db):
    return [dict(r) for r in db.get_connection().execute(
        "SELECT severity, title FROM alerts").fetchall()]


class TestSuccessPath:
    def test_none_return_records_success(self, guarded):
        run_guard.run_guarded("intraday_scan", lambda: None)
        rows = _runs(guarded)
        assert len(rows) == 1
        assert rows[0]["pipeline_name"] == "intraday_scan"
        assert rows[0]["status"] == "success"
        assert _alerts(guarded) == []

    def test_zero_exit_records_success(self, guarded):
        run_guard.run_guarded("api_health_check", lambda: 0)
        assert _runs(guarded)[0]["status"] == "success"

    def test_return_value_passthrough(self, guarded):
        assert run_guard.run_guarded("e2e", lambda: 5) == 5


class TestExceptionPath:
    def test_exception_records_failed_with_traceback(self, guarded):
        def boom():
            raise ValueError("disk full")

        with pytest.raises(ValueError, match="disk full"):
            run_guard.run_guarded("daily_update", boom)
        rows = _runs(guarded)
        assert rows[0]["status"] == "failed"
        assert "ValueError" in rows[0]["error_msg"]
        assert "disk full" in rows[0]["error_msg"]

    def test_exception_sends_critical_alert(self, guarded):
        def boom():
            raise RuntimeError("kaboom")

        with pytest.raises(RuntimeError):
            run_guard.run_guarded("daily_update", boom)
        alerts = _alerts(guarded)
        assert len(alerts) == 1
        assert alerts[0]["severity"] == "critical"
        assert "daily_update" in alerts[0]["title"]


class TestNonZeroExitPath:
    def test_nonzero_exit_records_failed_and_alerts(self, guarded):
        assert run_guard.run_guarded("e2e", lambda: 1) == 1
        rows = _runs(guarded)
        assert rows[0]["status"] == "failed"
        assert "exit code 1" in rows[0]["error_msg"]
        alerts = _alerts(guarded)
        assert len(alerts) == 1
        assert alerts[0]["severity"] == "warning"

    def test_nonzero_exit_no_alert_when_disabled(self, guarded):
        # e2e_verification / api_health_check 自带告警, 关闭哨兵告警避免重复
        run_guard.run_guarded("e2e", lambda: 2, alert_on_exit_code=False)
        assert _runs(guarded)[0]["status"] == "failed"
        assert _alerts(guarded) == []


class TestFailSafe:
    def test_observability_failure_does_not_mask_result(self, db, monkeypatch):
        # 记录链路炸了也不能影响脚本本身的返回值
        def broken_db():
            raise OSError("db unreachable")

        monkeypatch.setattr(run_guard, "_get_db", broken_db)
        assert run_guard.run_guarded("intraday_scan", lambda: 0) == 0

    def test_observability_failure_does_not_mask_exception(self, db, monkeypatch):
        def broken_db():
            raise OSError("db unreachable")

        monkeypatch.setattr(run_guard, "_get_db", broken_db)

        def boom():
            raise ValueError("original")

        with pytest.raises(ValueError, match="original"):
            run_guard.run_guarded("daily_update", boom)
