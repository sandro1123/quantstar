"""Dev verification consumes completed factor data, not the later evening report."""
from pathlib import Path

import pytest
import yaml

from common.database import Database
from ops_layer.alert_manager import AlertManager
from ops_layer.core.tables import OPS_TABLES_DDL
from ops_layer.orchestrator import PipelineOrchestrator
from ops_layer.pipeline_logger import PipelineLogger


@pytest.mark.parametrize("factor_status,allowed", [("success", True), ("failed", False), (None, False)])
def test_dev_verify_gate_without_evening_report(monkeypatch, factor_status, allowed):
    monkeypatch.setattr("ops_layer.orchestrator.bj_now", lambda: "2026-09-17 17:15:00")
    path = Path(__file__).resolve().parents[3] / "config/defaults/ops.yaml"
    pipelines = yaml.safe_load(path.read_text(encoding="utf-8"))["ops"]["pipelines"]
    db = Database(":memory:")
    conn = db.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    if factor_status is not None:
        conn.execute(
            "INSERT INTO pipeline_runs (run_id, pipeline_name, date, start_time, status) "
            "VALUES (?, ?, ?, ?, ?)",
            ("factor-run", "factor_update", "2026-09-17", "2026-09-17 16:00:00", factor_status),
        )
        conn.commit()
    orch = PipelineOrchestrator(db, PipelineLogger(db), AlertManager(db))
    orch._pipeline_configs = pipelines
    try:
        ok, reason = orch.can_run("dev_verify_report")
        assert ok is allowed, reason
    finally:
        orch.shutdown()
        db.close()
