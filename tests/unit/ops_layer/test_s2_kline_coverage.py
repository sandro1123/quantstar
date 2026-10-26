"""S2 F2.5 — kline 分表覆盖率判定回归测试 (防假成功)

场景: kline 子管道报 success 但当日落库行数 < min_kline_rows → 该日判 failed。
"""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock

from common.database import Database
from data_layer.pipelines.base import PipelineResult
from ops_layer.alert_manager import AlertManager
from ops_layer.orchestrator import PipelineOrchestrator
from ops_layer.pipeline_logger import PipelineLogger

DATE = "2026-08-16"


def _orchestrator():
    db = Database(":memory:")
    db.init_db()
    from ops_layer.core.tables import OPS_TABLES_DDL
    conn = db.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.commit()
    orch = PipelineOrchestrator(db, PipelineLogger(db), AlertManager(db))
    orch._pipeline_configs = {
        "daily_data_pipeline": {
            "max_retries": 0,
            "timeout_s": 2,
            "start_date": DATE,
            "end_date": DATE,
        }
    }
    orch.alert_manager.send = MagicMock(return_value=True)
    orch._executor.shutdown(wait=True)
    orch._executor = ThreadPoolExecutor(max_workers=1)
    return db, orch


def _seed_klines(db, n):
    conn = db.get_connection()
    conn.execute("CREATE TABLE IF NOT EXISTS daily_klines (code TEXT, date TEXT, close REAL)")
    conn.executemany(
        "INSERT INTO daily_klines(code, date, close) VALUES (?, ?, 10.0)",
        [(str(i).zfill(6), DATE) for i in range(n)],
    )
    conn.commit()


def _data_layer(db):
    def run_pipeline(name, pipeline_input):
        return PipelineResult(name=name, date=pipeline_input.date,
                              status="success", records_written=10, errors=[])
    return SimpleNamespace(db=db, run_pipeline=run_pipeline)


def test_kline_success_but_under_threshold_fails_parent():
    db, orch = _orchestrator()
    _seed_klines(db, 85)
    orch.set_app(SimpleNamespace(data=_data_layer(db)))
    result = orch.run_pipeline("daily_data_pipeline")
    assert result.status == "failed"
    assert "coverage insufficient" in result.error
    row = db.get_connection().execute(
        "SELECT status FROM pipeline_runs WHERE pipeline_name = ?",
        ("daily_data_pipeline",),
    ).fetchone()
    assert row["status"] == "failed"
    db.close()


def test_kline_above_threshold_passes():
    db, orch = _orchestrator()
    _seed_klines(db, 5500)
    orch.set_app(SimpleNamespace(data=_data_layer(db)))
    result = orch.run_pipeline("daily_data_pipeline")
    assert result.status == "success"
    db.close()


def test_min_kline_rows_config_override():
    db, orch = _orchestrator()
    _seed_klines(db, 50)

    cfg = SimpleNamespace(get=lambda key, default=None: 40 if key == "ops.min_kline_rows" else default)
    orch.config = cfg
    assert orch._min_kline_rows() == 40

    orch.set_app(SimpleNamespace(data=_data_layer(db)))
    result = orch.run_pipeline("daily_data_pipeline")
    assert result.status == "success", "阈值=40, 50 行应通过"
    db.close()
