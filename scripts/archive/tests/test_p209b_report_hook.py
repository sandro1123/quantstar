from __future__ import annotations

from report_layer import ReportLayer
from ops_layer.core.models import HealthCheck, LayerHealth


class FakeDualRead:
    def __init__(self, status="green"):
        self.status = status

    def metadata(self):
        return {
            "enabled": True,
            "started": True,
            "preflight": {"status": "PASS_DUAL_READ_PREFLIGHT"},
            "summary": {"blocking": 0 if self.status == "green" else 1},
            "error": None,
        }

    def health(self):
        return LayerHealth(
            layer="valuation_dual_read",
            status=self.status,
            checks=[HealthCheck(name="probe", status="pass", detail="ok")],
            summary=f"dual-read-{self.status}",
        )


def test_report_dual_read_metadata_is_default_off():
    report = ReportLayer()
    assert report.dual_read_metadata() == {"enabled": False}
    health = report.health()
    assert all(check.name != "valuation_dual_read" for check in health.checks)


def test_report_exposes_metadata_without_changing_message_contract():
    service = FakeDualRead("green")
    report = ReportLayer(valuation_dual_read=service)
    metadata = report.dual_read_metadata()
    health = report.health()

    assert metadata["enabled"] is True
    dual_checks = [check for check in health.checks if check.name == "valuation_dual_read"]
    assert len(dual_checks) == 1
    assert dual_checks[0].status == "pass"


def test_report_health_propagates_yellow_and_red_dual_read_state():
    yellow = ReportLayer(valuation_dual_read=FakeDualRead("yellow")).health()
    red = ReportLayer(valuation_dual_read=FakeDualRead("red")).health()

    assert yellow.status == "yellow"
    assert red.status == "red"
