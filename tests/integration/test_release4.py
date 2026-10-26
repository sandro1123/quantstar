"""Release 4 集成测试"""
import pytest
from common.database import Database
from ops_layer import OpsLayer
from ops_layer.orchestrator import PipelineOrchestrator


def test_ops_full_flow():
    db = Database(":memory:")
    db.init_db()
    ol = OpsLayer(db=db)
    ol.start()
    # 健康检查
    report = ol.check_health()
    assert report.overall_status in ("green", "yellow", "red")
    # 管道日志
    run_id = ol.start_pipeline("daily_data")
    ol.end_pipeline(run_id, "success")
    # 告警
    ol.alert("info", "ops", "系统启动", "所有层正常")
    # 版本
    info = ol.version_info()
    assert info.code_version
    ol.shutdown()
    db.close()


def test_orchestrator():
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
    # 无依赖时可以运行
    can, reason = orch.can_run("test", None)
    assert can
    # 有未完成依赖时不能运行
    can, reason = orch.can_run("test", ["dependency1"])
    assert not can
    db.close()
