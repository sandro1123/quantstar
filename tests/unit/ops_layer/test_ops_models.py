"""Tests for ops_layer/core/models.py"""
import pytest
from ops_layer.core.models import (
    HealthCheck, LayerHealth, AttentionItem, OpsReport,
    Alert, PipelineRun, StartupReport,
)


def test_health_check_defaults():
    hc = HealthCheck(name="db", status="pass", detail="ok")
    assert hc.metric is None
    assert hc.threshold is None


def test_health_check_with_metrics():
    hc = HealthCheck(name="db", status="pass", detail="ok", metric=95.0, threshold=80.0)
    assert hc.metric == 95.0
    assert hc.threshold == 80.0


def test_layer_health_defaults():
    lh = LayerHealth(layer="data", status="green")
    assert lh.checks == []
    assert lh.summary == ""


def test_layer_health_with_checks():
    checks = [HealthCheck(name="db", status="pass", detail="ok")]
    lh = LayerHealth(layer="data", status="green", checks=checks)
    assert len(lh.checks) == 1


def test_attention_item_defaults():
    ai = AttentionItem(severity="warning", layer="data", message="disk low")
    assert ai.action is None


def test_attention_item_with_action():
    ai = AttentionItem(severity="critical", layer="ops", message="down", action="restart")
    assert ai.action == "restart"


def test_ops_report_defaults():
    r = OpsReport(date="2026-07-18", overall_status="green")
    assert r.layers == []
    assert r.needs_attention == []
    assert r.summary == ""


def test_alert_defaults():
    a = Alert(severity="warning", layer="data", title="low disk", detail="90%")
    assert a.action is None
    assert a.created_at == ""
    assert a.status == "open"


def test_pipeline_run_defaults():
    pr = PipelineRun(run_id="r1", pipeline_name="kline", date="2026-07-18",
                     start_time="2026-07-18 10:00:00")
    assert pr.end_time is None
    assert pr.status == "running"
    assert pr.error_msg == ""
    assert pr.duration_s == 0.0
    assert pr.version == ""
    assert pr.config_hash == ""


def test_startup_report_defaults():
    sr = StartupReport(success=True)
    assert sr.layers == []
    assert sr.failures == []
