"""ops_layer/startup.py — 启动管理"""

from common.database import Database
from ops_layer.alert_manager import AlertManager
from ops_layer.core.models import StartupReport
from ops_layer.pipeline_logger import PipelineLogger


class StartupManager:
    CRITICAL_LAYERS = ["config", "data"]
    NON_CRITICAL_LAYERS = [
        "strategy", "agent", "execution", "backtest",
        "report", "delivery", "ops",
    ]

    def __init__(self, db: Database, pipeline_logger: PipelineLogger, alert_manager: AlertManager):
        self.db = db
        self.pipeline_logger = pipeline_logger
        self.alert_manager = alert_manager

    def startup(self, layer_instances: dict[str, object]) -> StartupReport:
        layers_report = []
        failures = []
        for name, layer in layer_instances.items():
            try:
                if hasattr(layer, "start"):
                    layer.start()
                layers_report.append({"layer": name, "status": "started"})
            except Exception as e:
                layers_report.append({"layer": name, "status": "failed", "error": str(e)})
                if name in self.CRITICAL_LAYERS:
                    failures.append(name)

        success = len(failures) == 0
        return StartupReport(success=success, layers=layers_report, failures=failures)
