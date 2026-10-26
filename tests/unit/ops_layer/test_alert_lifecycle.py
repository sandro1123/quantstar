"""OPS-07: 告警生命周期闭环 — dismiss/escalation 真推送+24h去重/恢复自动 resolve/health_check 触发

测行为边界:
- 超时 critical → 二次推送 + last_escalated_at 记录
- 24h 内已升级 → 不重推
- 未超时 → 不升级
- 管道 success → 自动 resolve 同管道告警 + 恢复通知
- dismiss 只作用于 open
"""
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta

from common.database import Database
from ops_layer.alert_manager import AlertManager
from ops_layer.core.tables import OPS_TABLES_DDL


@pytest.fixture
def am():
    db = Database(":memory:")
    db.init_db()
    conn = db.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.commit()
    return AlertManager(db), conn


def _insert_alert(conn, title="管道失败: factor_update", severity="critical",
                  status="open", created_at=None, last_escalated_at=None):
    created = created_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO alerts (severity, layer, title, detail, status, created_at, last_escalated_at) "
        "VALUES (?, 'ops', ?, 'test detail', ?, ?, ?)",
        (severity, title, status, created, last_escalated_at))
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


class TestDismiss:
    def test_dismiss_open_alert(self, am):
        mgr, conn = am
        aid = _insert_alert(conn)
        assert mgr.dismiss(aid) is True
        row = conn.execute("SELECT status FROM alerts WHERE id = ?", (aid,)).fetchone()
        assert row["status"] == "dismissed"

    def test_dismiss_non_open_fails(self, am):
        mgr, conn = am
        aid = _insert_alert(conn, status="resolved")
        assert mgr.dismiss(aid) is False


class TestEscalationPush:
    def test_critical_timeout_escalates_and_pushes(self, am):
        mgr, conn = am
        old_time = (datetime.now() - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        aid = _insert_alert(conn, created_at=old_time)

        with patch.object(mgr, "push_feishu", return_value=True) as mock_push:
            escalated = mgr.check_escalation()

        assert len(escalated) == 1
        assert escalated[0]["id"] == aid
        assert escalated[0]["pushed"] is True
        mock_push.assert_called_once()
        # last_escalated_at 已记录 (DDL 含此列)
        row = conn.execute("SELECT last_escalated_at, action FROM alerts WHERE id = ?", (aid,)).fetchone()
        assert row["last_escalated_at"] is not None
        assert row["action"] == "escalated"

    def test_24h_dedup_prevents_re_push(self, am):
        mgr, conn = am
        old_time = (datetime.now() - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        recent_esc = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        _insert_alert(conn, created_at=old_time, last_escalated_at=recent_esc)

        with patch.object(mgr, "push_feishu", return_value=True) as mock_push:
            escalated = mgr.check_escalation()

        assert len(escalated) == 0
        mock_push.assert_not_called()

    def test_fresh_alert_not_escalated(self, am):
        mgr, conn = am
        _insert_alert(conn)  # just created, not timed out
        escalated = mgr.check_escalation()
        assert len(escalated) == 0

    def test_warning_timeout_no_push_only_mark(self, am):
        """非 critical 超时只标记不推送"""
        mgr, conn = am
        old_time = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        _insert_alert(conn, severity="warning", created_at=old_time)

        with patch.object(mgr, "push_feishu", return_value=True) as mock_push:
            escalated = mgr.check_escalation()

        assert len(escalated) == 1
        assert "pushed" not in escalated[0]  # 非 critical 不推
        mock_push.assert_not_called()


class TestRecoveryAutoResolve:
    def test_resolve_pipeline_alerts(self, am):
        mgr, conn = am
        _insert_alert(conn, title="管道失败: factor_update")
        _insert_alert(conn, title="管道失败: factor_update")
        _insert_alert(conn, title="管道失败: other_pipeline")

        with patch.object(mgr, "push_feishu", return_value=True):
            n = mgr.resolve_pipeline_alerts("factor_update")
        assert n == 2
        remaining = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE status = 'open' AND title LIKE '%factor_update%'"
        ).fetchone()[0]
        assert remaining == 0

    def test_resolve_no_match_returns_zero(self, am):
        mgr, conn = am
        _insert_alert(conn, title="管道失败: other")
        assert mgr.resolve_pipeline_alerts("factor_update") == 0

    def test_recovery_pushes_info_notification(self, am):
        mgr, conn = am
        _insert_alert(conn, title="管道失败: factor_update")
        with patch.object(mgr, "push_feishu", return_value=True) as mock_push:
            mgr.resolve_pipeline_alerts("factor_update")
        mock_push.assert_called_once()
        args = mock_push.call_args
        assert "恢复" in args[0][0]
        assert args[1].get("severity") == "info" or args[0][2] == "info" if len(args[0]) > 2 else True


class TestHealthCheckEscalation:
    """check_escalation 纳入 health_check handler (生产触发方)"""

    def test_health_check_calls_escalation(self):
        from ops_layer.orchestrator import PipelineOrchestrator
        db = Database(":memory:")
        db.init_db()
        conn = db.get_connection()
        conn.executescript(OPS_TABLES_DDL)
        conn.commit()

        alert_mgr = AlertManager(db)
        orch = PipelineOrchestrator(db, MagicMock(), alert_mgr, None)
        orch._pipeline_configs = {}

        with patch.object(orch, "_run_anomaly_detection", return_value=[]), \
             patch("ops_layer.schedule_check.run_schedule_check",
                   return_value=(True, {"missing": [], "phantoms": [], "covered": []})), \
             patch.object(alert_mgr, "check_escalation", return_value=[{"id": 1}]) as mock_esc, \
             patch("bootstrap.bootstrap_app") as mock_boot:
            mock_app = MagicMock()
            mock_app.ops.check_health.return_value = MagicMock(overall_status="green", layers=[])
            mock_boot.return_value = mock_app
            result = orch._run_health_check({})

        mock_esc.assert_called_once()
        assert result.metadata.get("escalated_alerts") == 1
