"""Tests for ops_layer/health.py"""
import pytest
from common.database import Database
from ops_layer.health import OpsHealth
from ops_layer.core.models import LayerHealth, HealthCheck


class MockHealthyLayer:
    def health(self) -> LayerHealth:
        return LayerHealth(
            layer="data",
            status="green",
            checks=[HealthCheck(name="db", status="pass", detail="ok")],
            summary="all good",
        )


class MockUnhealthyLayer:
    def health(self) -> LayerHealth:
        return LayerHealth(
            layer="strategy",
            status="red",
            checks=[HealthCheck(name="model", status="fail", detail="broken")],
            summary="model broken",
        )


class MockWarningLayer:
    def health(self) -> LayerHealth:
        return LayerHealth(
            layer="execution",
            status="yellow",
            checks=[HealthCheck(name="broker", status="warn", detail="slow")],
            summary="slow response",
        )


class MockNoHealthLayer:
    pass


class MockFailingHealthLayer:
    def health(self):
        raise RuntimeError("health check crashed")


def test_check_all_green():
    h = OpsHealth({"data": MockHealthyLayer()})
    report = h.check_all()
    assert report.overall_status == "green"
    assert len(report.layers) == 1
    assert report.layers[0].status == "green"


def test_check_all_red():
    h = OpsHealth({"data": MockHealthyLayer(), "strategy": MockUnhealthyLayer()})
    report = h.check_all()
    assert report.overall_status == "red"
    assert len(report.layers) == 2


def test_check_all_yellow():
    h = OpsHealth({"execution": MockWarningLayer()})
    report = h.check_all()
    assert report.overall_status == "yellow"


def test_check_all_with_no_health_interface():
    h = OpsHealth({"delivery": MockNoHealthLayer()})
    report = h.check_all()
    assert report.overall_status == "red"
    assert report.layers[0].summary == "缺少健康检查接口"
    assert report.needs_attention[0].severity == "critical"


def test_check_all_with_exception():
    h = OpsHealth({"ops": MockFailingHealthLayer()})
    report = h.check_all()
    assert report.overall_status == "red"
    assert len(report.needs_attention) == 1
    assert report.needs_attention[0].severity == "critical"


def test_empty_layers():
    h = OpsHealth({})
    report = h.check_all()
    assert report.overall_status == "green"
    assert len(report.layers) == 0
