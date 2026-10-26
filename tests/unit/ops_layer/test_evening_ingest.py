"""晚间补采通过真实 dispatch 聚合两条腿，不重跑整个 DDP。"""
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from common.database import Database
from data_layer.pipelines.base import PipelineResult
from ops_layer.alert_manager import AlertManager
from ops_layer.orchestrator import PipelineOrchestrator
from ops_layer.pipeline_logger import PipelineLogger


@pytest.mark.parametrize("index_status,lhb_status,expected", [
    ("success", "success", "success"),
    ("partial", "success", "partial"),
    ("success", "failed", "failed"),
])
def test_evening_ingest_preserves_leg_status_and_business_date(index_status, lhb_status, expected):
    db = Database(":memory:")
    calls = []

    def run_pipeline(name, payload):
        calls.append((name, payload.date, payload.db))
        status = index_status if name == "index_daily" else lhb_status
        return PipelineResult(name=name, date=payload.date, status=status,
                              records_written=2 if status == "success" else 0,
                              errors=[] if status == "success" else ["not ready"])

    data = SimpleNamespace(db=db, pipelines={"index_daily": object(), "lhb_daily": object()},
                           run_pipeline=run_pipeline)
    orch = PipelineOrchestrator(db, PipelineLogger(db), AlertManager(db))
    orch.set_app(SimpleNamespace(data=data))
    try:
        result = orch._execute_pipeline("evening_ingest", {"date": "2026-09-17"})
        assert result.final_status == expected
        assert calls == [("index_daily", "2026-09-17", db), ("lhb_daily", "2026-09-17", db)]
        assert result.metadata["date"] == "2026-09-17"
    finally:
        orch.shutdown()
        db.close()


def test_evening_ingest_config_does_not_wait_for_failed_ddp():
    path = Path(__file__).resolve().parents[3] / "config/defaults/ops.yaml"
    pipelines = yaml.safe_load(path.read_text(encoding="utf-8"))["ops"]["pipelines"]
    db = Database(":memory:")
    orch = PipelineOrchestrator(db, PipelineLogger(db), AlertManager(db))
    try:
        assert "evening_ingest" in pipelines
        orch._pipeline_configs = pipelines
        assert orch.can_run("evening_ingest")[0] is True
    finally:
        orch.shutdown()
        db.close()
