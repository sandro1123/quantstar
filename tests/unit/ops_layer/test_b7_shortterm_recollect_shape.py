"""B7 单元与契约测试: _run_shortterm_recollect 必须返回 HandlerResult 并通过 :771 契约校验。"""

from types import SimpleNamespace

from common.database import Database
from data_layer.pipelines.base import PipelineResult
from ops_layer.alert_manager import AlertManager
from ops_layer.core.models import HandlerResult
from ops_layer.orchestrator import PipelineOrchestrator
from ops_layer.pipeline_logger import PipelineLogger


def _orchestrator():
    db = Database(":memory:")
    db.init_db()
    from ops_layer.core.tables import OPS_TABLES_DDL

    conn = db.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.commit()
    return db, PipelineOrchestrator(db, PipelineLogger(db), AlertManager(db))


def _fake_app(db, registered, run_pipeline):
    data_layer = SimpleNamespace(db=db, run_pipeline=run_pipeline,
                                 pipelines=registered)
    return SimpleNamespace(data=data_layer)


def _ok_run_pipeline(name, pipeline_input):
    return PipelineResult(name=name, date=pipeline_input.date,
                          status="success", records_written=100,
                          metadata={"collected": ["anomaly:100"],
                                    "recollect_overwrote": ["t: fp a->b"]})


def test_returns_handler_result_with_records_and_overwrite_trace(monkeypatch):
    db, orchestrator = _orchestrator()
    app = _fake_app(db, {"shortterm_recollect": object()}, _ok_run_pipeline)
    monkeypatch.setattr("bootstrap.bootstrap_app", lambda: app)

    result = orchestrator._run_shortterm_recollect({})

    assert isinstance(result, HandlerResult), type(result)
    assert result.success is True
    assert result.records_written == 100
    assert result.metadata["recollect_overwrote"] == ["t: fp a->b"]
    assert result.metadata["elapsed_s"] >= 0
    db.close()


def test_failed_status_maps_to_failure(monkeypatch):
    db, orchestrator = _orchestrator()

    def run_pipeline(name, pipeline_input):
        return PipelineResult(name=name, date=pipeline_input.date,
                              status="failed",
                              errors=["anomaly: 补采无数据"])

    app = _fake_app(db, {"shortterm_recollect": object()}, run_pipeline)
    monkeypatch.setattr("bootstrap.bootstrap_app", lambda: app)

    result = orchestrator._run_shortterm_recollect({})

    assert isinstance(result, HandlerResult), type(result)
    assert result.success is False
    assert result.errors, "失败必须留 errors 痕迹"
    db.close()


def test_partial_status_keeps_success_with_partial_flag(monkeypatch):
    db, orchestrator = _orchestrator()

    def run_pipeline(name, pipeline_input):
        return PipelineResult(name=name, date=pipeline_input.date,
                              status="partial", records_written=50,
                              errors=["lhb: 补采无数据"])

    app = _fake_app(db, {"shortterm_recollect": object()}, run_pipeline)
    monkeypatch.setattr("bootstrap.bootstrap_app", lambda: app)

    result = orchestrator._run_shortterm_recollect({})

    assert isinstance(result, HandlerResult), type(result)
    assert result.success is True
    assert result.metadata.get("partial") is True
    assert result.records_written == 50
    db.close()


def test_skipped_when_pipeline_unregistered(monkeypatch):
    db, orchestrator = _orchestrator()
    app = _fake_app(db, {}, _ok_run_pipeline)
    monkeypatch.setattr("bootstrap.bootstrap_app", lambda: app)

    result = orchestrator._run_shortterm_recollect({})

    assert isinstance(result, HandlerResult), type(result)
    assert result.success is True
    assert result.metadata.get("skipped") is True
    db.close()


def test_run_pipeline_contract_check_passes(monkeypatch):
    """契约钉: run_pipeline 经 :771 契约校验必须返回 success 状态，不可 TypeError 失败。"""
    db, orchestrator = _orchestrator()
    app = _fake_app(db, {"shortterm_recollect": object()}, _ok_run_pipeline)
    monkeypatch.setattr("bootstrap.bootstrap_app", lambda: app)
    orchestrator._pipeline_configs = {"shortterm_recollect": {"timeout_s": 10}}

    run_res = orchestrator.run_pipeline("shortterm_recollect")

    assert run_res.status == "success", f"期望 success, 实得 {run_res.status}, error={run_res.error}"
    assert run_res.metadata["recollect_overwrote"] == ["t: fp a->b"]
    db.close()
