"""CLI + PipelineRunResult + HandlerResult 集成测试"""
import pytest
from ops_layer.core.models import PipelineRunResult, HandlerResult
from ops_layer.orchestrator import PipelineOrchestrator
from common.database import Database


def test_pipeline_run_result_is_dataclass():
    result = PipelineRunResult(name="test", status="success")
    assert result.name == "test"
    assert result.status == "success"
    with pytest.raises(AttributeError):
        _ = result.get("status", "unknown")


def test_handler_result_propagates_failure():
    db = Database(":memory:")
    db.init_db()
    from ops_layer.core.tables import OPS_TABLES_DDL
    conn = db.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.commit()
    from ops_layer.pipeline_logger import PipelineLogger
    from ops_layer.alert_manager import AlertManager
    pl = PipelineLogger(db)
    am = AlertManager(db)
    orch = PipelineOrchestrator(db, pl, am)
    assert not HandlerResult(success=False, errors=["kline failed"]).success
    assert HandlerResult(success=True).success


def test_handler_result_defaults():
    hr = HandlerResult()
    assert hr.success
    assert hr.records_written == 0
    assert hr.errors == []


def test_cli_imports():
    from cli.main import cli, health, run
    assert cli is not None
    assert health is not None
