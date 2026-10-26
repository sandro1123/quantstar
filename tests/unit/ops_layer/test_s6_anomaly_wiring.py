"""F6.3 异常检测接线测试 — anomaly_detector DB 持久化路径 + orchestrator 接线

覆盖:
- record_health 写入 health_history
- check_health_red_db: 跨进程连续红色判定 (不足阈值/连续红/混绿)
- scan_recent_runs: 连续失败检测
- orchestrator._run_anomaly_detection: 异常 → AlertManager + fail-safe
"""
import pytest

from common.database import Database
from ops_layer.alert_manager import AlertManager
from ops_layer.anomaly_detector import AnomalyDetector
from ops_layer.core.tables import OPS_TABLES_DDL
from ops_layer.orchestrator import PipelineOrchestrator
from ops_layer.pipeline_logger import PipelineLogger


@pytest.fixture
def db():
    database = Database(":memory:")
    conn = database.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.commit()
    yield database
    database.close()


def _health_rows(db):
    return [dict(r) for r in db.get_connection().execute(
        "SELECT overall_status FROM health_history ORDER BY id").fetchall()]


def _alerts(db):
    return [dict(r) for r in db.get_connection().execute(
        "SELECT severity, title FROM alerts").fetchall()]


class TestRecordHealth:
    def test_writes_history_row(self, db):
        AnomalyDetector(db).record_health("green", {"data": "green"})
        rows = _health_rows(db)
        assert len(rows) == 1
        assert rows[0]["overall_status"] == "green"

    def test_multiple_records_preserve_order(self, db):
        det = AnomalyDetector(db)
        for s in ["green", "red", "red"]:
            det.record_health(s)
        assert [r["overall_status"] for r in _health_rows(db)] == ["green", "red", "red"]


class TestCheckHealthRedDb:
    def test_not_enough_history_returns_none(self, db):
        det = AnomalyDetector(db)
        det.record_health("red")
        det.record_health("red")
        assert det.check_health_red_db() is None  # 只有 2 条 < 阈值 3

    def test_consecutive_red_triggers(self, db):
        det = AnomalyDetector(db)
        for _ in range(3):
            det.record_health("red")
        result = det.check_health_red_db()
        assert result is not None
        assert result["anomaly_type"] == "health_red"
        assert result["severity"] == "critical"

    def test_mixed_green_does_not_trigger(self, db):
        det = AnomalyDetector(db)
        for s in ["red", "green", "red"]:
            det.record_health(s)
        assert det.check_health_red_db() is None

    def test_yellow_does_not_trigger(self, db):
        det = AnomalyDetector(db)
        for s in ["red", "red", "yellow"]:
            det.record_health(s)
        assert det.check_health_red_db() is None


class TestScanRecentRuns:
    def _insert_run(self, db, name, status, start_time):
        db.get_connection().execute(
            "INSERT INTO pipeline_runs (run_id, pipeline_name, date, start_time, status) "
            "VALUES (?, ?, ?, ?, ?)",
            (f"{name}-{start_time}", name, start_time[:10], start_time, status),
        )
        db.get_connection().commit()

    def test_consecutive_failures_detected(self, db):
        for i in range(3):
            self._insert_run(db, "factor_update", "failed", f"2026-08-17 10:0{i}:00")
        anomalies = AnomalyDetector(db).scan_recent_runs()
        assert len(anomalies) == 1
        assert anomalies[0]["anomaly_type"] == "consecutive_failures"
        assert anomalies[0]["pipeline"] == "factor_update"
        assert anomalies[0]["severity"] == "critical"

    def test_success_breaks_streak(self, db):
        self._insert_run(db, "factor_update", "failed", "2026-08-17 10:00:00")
        self._insert_run(db, "factor_update", "success", "2026-08-17 10:01:00")
        self._insert_run(db, "factor_update", "failed", "2026-08-17 10:02:00")
        assert AnomalyDetector(db).scan_recent_runs() == []

    def test_empty_table_returns_empty(self, db):
        assert AnomalyDetector(db).scan_recent_runs() == []


class TestOrchestratorWiring:
    def _orchestrator(self, db, monkeypatch):
        monkeypatch.setattr(AlertManager, "push_feishu", lambda self, *a, **k: True)
        return PipelineOrchestrator(db, PipelineLogger(db), AlertManager(db))

    def test_anomaly_sends_alert(self, db, monkeypatch):
        orch = self._orchestrator(db, monkeypatch)
        # 预置 3 次红色健康历史 → 触发 health_red
        det = AnomalyDetector(db)
        for _ in range(3):
            det.record_health("red")

        from types import SimpleNamespace
        report = SimpleNamespace(overall_status="red", layers=[])
        anomalies = orch._run_anomaly_detection(report)
        assert len(anomalies) >= 1
        alerts = _alerts(db)
        assert any("health_red" in a["title"] for a in alerts)

    def test_green_no_alert(self, db, monkeypatch):
        orch = self._orchestrator(db, monkeypatch)
        from types import SimpleNamespace
        report = SimpleNamespace(overall_status="green", layers=[])
        anomalies = orch._run_anomaly_detection(report)
        assert anomalies == []
        assert _alerts(db) == []

    def test_records_health_history(self, db, monkeypatch):
        orch = self._orchestrator(db, monkeypatch)
        from types import SimpleNamespace
        report = SimpleNamespace(overall_status="yellow",
                                 layers=[SimpleNamespace(layer="data", status="yellow")])
        orch._run_anomaly_detection(report)
        rows = _health_rows(db)
        assert len(rows) == 1
        assert rows[0]["overall_status"] == "yellow"

    def test_fail_safe_on_detector_error(self, db, monkeypatch):
        orch = self._orchestrator(db, monkeypatch)
        # 让 AnomalyDetector 构造即抛错, 验证不影响健康检查主流程
        import ops_layer.anomaly_detector as ad_mod

        def broken(*a, **k):
            raise OSError("db gone")

        monkeypatch.setattr(ad_mod, "AnomalyDetector", broken)
        from types import SimpleNamespace
        report = SimpleNamespace(overall_status="green", layers=[])
        # 不应抛异常
        assert orch._run_anomaly_detection(report) == []
