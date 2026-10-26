"""Ops Layer 单元测试"""
import pytest
from common.database import Database
from ops_layer import OpsLayer
from ops_layer.auto_healer import AutoHealer


@pytest.fixture
def ops_layer():
    db = Database(":memory:")
    db.init_db()
    ol = OpsLayer(db=db)
    ol.start()
    yield ol
    ol.shutdown()
    db.close()


def test_ops_start(ops_layer):
    assert ops_layer._started


def test_health_check(ops_layer):
    report = ops_layer.check_health()
    assert report.overall_status in ("green", "yellow", "red")
    assert report.date


def test_pipeline_logger(ops_layer):
    run_id = ops_layer.start_pipeline("test_pipeline")
    assert run_id
    ops_layer.end_pipeline(run_id, "success")
    runs = ops_layer.pipeline_logger.get_recent_runs(days=1)
    assert len(runs) >= 1


def test_alert_manager(ops_layer):
    result = ops_layer.alert("warning", "test", "测试告警", "这是一个测试")
    assert result


def test_alert_dedup(ops_layer):
    ops_layer.alert("warning", "test", "去重测试", "第一次")
    result = ops_layer.alert("warning", "test", "去重测试", "第二次")
    assert not result  # 应该被去重


def test_version_tracker(ops_layer):
    info = ops_layer.version_info()
    assert info.code_version
    assert info.deployed_at


class TestOpsLayerDecorators:
    def test_start_has_catch_decorator(self):
        import ops_layer
        method = ops_layer.OpsLayer.start
        assert hasattr(method, '__wrapped__')

    def test_check_health_has_catch_decorator(self):
        import ops_layer
        method = ops_layer.OpsLayer.check_health
        assert hasattr(method, '__wrapped__')

    def test_run_pipeline_has_catch_decorator(self):
        import ops_layer
        method = ops_layer.OpsLayer.run_pipeline
        assert hasattr(method, '__wrapped__')

    def test_alert_has_catch_decorator(self):
        import ops_layer
        method = ops_layer.OpsLayer.alert
        assert hasattr(method, '__wrapped__')

    def test_shutdown_has_catch_decorator(self):
        import ops_layer
        method = ops_layer.OpsLayer.shutdown
        assert hasattr(method, '__wrapped__')

    def test_start_pipeline_has_catch_decorator(self):
        import ops_layer
        method = ops_layer.OpsLayer.start_pipeline
        assert hasattr(method, '__wrapped__')


class TestAutoHealerSQL:
    def _setup_db(self):
        db = Database(":memory:")
        db.init_db()
        conn = db.get_connection()
        from ops_layer.core.tables import OPS_TABLES_DDL
        conn.executescript(OPS_TABLES_DDL)
        conn.commit()
        return db

    def test_heal_purge_old_data_valid_table(self):
        db = self._setup_db()
        healer = AutoHealer(db)
        anomaly = {"table": "pipeline_runs", "anomaly_type": "data_volume"}
        result = healer.heal(anomaly)
        assert result["status"] == "success"
        db.close()

    def test_heal_purge_old_data_invalid_table(self):
        db = self._setup_db()
        healer = AutoHealer(db)
        anomaly = {"table": "pipeline_runs; DROP TABLE alerts", "anomaly_type": "data_volume"}
        result = healer.heal(anomaly)
        assert result["status"] == "skipped"
        db.close()

    def test_heal_purge_old_data_empty_table(self):
        db = self._setup_db()
        healer = AutoHealer(db)
        anomaly = {"table": "", "anomaly_type": "data_volume"}
        result = healer.heal(anomaly)
        assert result["status"] == "skipped"
        db.close()


class TestPipelineConcurrency:
    """回归: 同管道并发触发必须 skip, 防止 cron 与手动触发同时写正式库。"""

    def test_same_pipeline_active_is_skipped(self, ops_layer):
        # 在 DB 中构造一个 running 的 kline 管道
        run_id = ops_layer.pipeline_logger.start_run("kline_daily")
        # orchestrator 不应被调用
        ops_layer.orchestrator.run_pipeline = None
        result = ops_layer.run_pipeline("kline_daily")
        assert result.status == "skipped"
        assert "already running" in result.error
        # 清理
        ops_layer.pipeline_logger.end_run(run_id, "success")

    def test_different_pipeline_not_blocked(self, ops_layer):
        import unittest.mock as mock
        run_id = ops_layer.pipeline_logger.start_run("kline_daily")
        ops_layer.orchestrator.run_pipeline = mock.Mock(return_value=ops_layer.orchestrator.run_pipeline)
        # 不同管道不受影响
        with mock.patch.object(ops_layer.orchestrator, "run_pipeline",
                               return_value=mock.Mock(status="success")) as mocked:
            mock_result = ops_layer.run_pipeline("financial_daily")
            assert mocked.called
            assert mock_result.status == "success"
        ops_layer.pipeline_logger.end_run(run_id, "success")

    def test_sequential_after_completion(self, ops_layer):
        import unittest.mock as mock
        # 一个 running 的管道结束后再次触发应进入 orchestrator
        run_id = ops_layer.pipeline_logger.start_run("kline_daily")
        ops_layer.pipeline_logger.end_run(run_id, "success")
        with mock.patch.object(ops_layer.orchestrator, "run_pipeline",
                               return_value=mock.Mock(status="success")) as mocked:
            ops_layer.run_pipeline("kline_daily")
            assert mocked.called
