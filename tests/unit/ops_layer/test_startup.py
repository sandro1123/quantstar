"""Tests for ops_layer/startup.py"""
import pytest
from common.database import Database
from ops_layer.core.tables import OPS_TABLES_DDL
from ops_layer.pipeline_logger import PipelineLogger
from ops_layer.alert_manager import AlertManager
from ops_layer.startup import StartupManager


@pytest.fixture
def db():
    database = Database(":memory:")
    conn = database.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.commit()
    yield database
    database.close()


def test_startup_all_ok(db):
    pl = PipelineLogger(db)
    am = AlertManager(db)
    sm = StartupManager(db, pl, am)
    layers = {"data": object(), "strategy": object()}
    report = sm.startup(layers)
    assert report.success is True
    assert len(report.layers) == 2


def test_startup_critical_failure(db):
    pl = PipelineLogger(db)
    am = AlertManager(db)

    class FailingLayer:
        def start(self):
            raise RuntimeError("cannot start")

    sm = StartupManager(db, pl, am)
    layers = {"config": FailingLayer()}
    report = sm.startup(layers)
    assert report.success is False
    assert "config" in report.failures


def test_startup_non_critical_failure(db):
    pl = PipelineLogger(db)
    am = AlertManager(db)

    class FailingLayer:
        def start(self):
            raise RuntimeError("cannot start")

    sm = StartupManager(db, pl, am)
    layers = {"strategy": FailingLayer()}
    report = sm.startup(layers)
    assert report.success is True
    assert "strategy" not in report.failures


def test_startup_no_start_method(db):
    pl = PipelineLogger(db)
    am = AlertManager(db)
    sm = StartupManager(db, pl, am)
    layers = {"delivery": object()}
    report = sm.startup(layers)
    assert report.success is True
    assert report.layers[0]["status"] == "started"
