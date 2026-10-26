"""自动修复器 — 重试/切换/回滚/清理/重建

排队态 (D-30, 2026-08-20 裁决): switch_source runbook 未接线 —
接线需先真建 runbook 文档与切换逻辑, 并经用户 GO 后方可启用。
接线前 _heal_switch_source 诚实返回 not_implemented, 不伪造切换成功。
"""

import re

from common.database import Database
from common.logging import get_logger

logger = get_logger("ops.healer")

_IDENTIFIER_RE = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')


class AutoHealer:
    def __init__(self, db: Database, orchestrator=None, alert_manager=None):
        self.db = db
        self.orchestrator = orchestrator
        self.alert_manager = alert_manager
        self._runbooks = {
            "pipeline_timeout": self._heal_restart_pipeline,
            "pipeline_failure": self._heal_restart_pipeline,
            "data_source_down": self._heal_switch_source,
            "health_red": self._heal_log_and_alert,
            "data_lag": self._heal_replay_data,
            "source_stale": self._heal_switch_source,
            "baseline_deviation": self._heal_reset_baseline,
            "data_volume": self._heal_purge_old_data,
            "consecutive_failures": self._heal_failover,
        }

    def heal(self, anomaly: dict) -> dict:
        anomaly_type = anomaly.get("anomaly_type", "")
        handler = self._runbooks.get(anomaly_type)
        if handler:
            logger.info(f"AutoHeal: running {anomaly_type} handler")
            return handler(anomaly)
        return {"action": "no_handler", "status": "skipped", "detail": f"无修复策略: {anomaly_type}"}

    def _heal_restart_pipeline(self, anomaly: dict) -> dict:
        pipeline = anomaly.get("pipeline", "")
        if self.orchestrator and pipeline:
            try:
                result = self.orchestrator.run_pipeline(pipeline)
                status = "success" if getattr(result, 'status', '') == "success" else "failed"
                return {"action": "restart_pipeline", "target": pipeline, "status": status}
            except Exception as e:
                return {"action": "restart_pipeline", "target": pipeline, "status": "failed", "error": str(e)}
        return {"action": "restart_pipeline", "target": pipeline, "status": "skipped", "detail": "无 orchestrator"}

    def _heal_switch_source(self, anomaly: dict) -> dict:
        """D-30 冻结: 切换 runbook 未接线, 仅记录 — 不伪造切换成功事实。"""
        source = anomaly.get("source", anomaly.get("pipeline", ""))
        logger.info(f"switch_source requested for {source}: runbook 未接线, 仅记录")
        conn = self.db.get_connection()
        conn.execute(
            "INSERT INTO action_log (action, target, status, detail) VALUES (?, ?, ?, ?)",
            ("switch_source", source, "not_implemented", "runbook 未接线, 仅记录 (D-30 冻结)")
        )
        conn.commit()
        return {"action": "switch_source", "target": source, "status": "not_implemented",
                "detail": "runbook 未接线, 仅记录"}

    def _heal_log_and_alert(self, anomaly: dict) -> dict:
        logger.warning(f"Health anomaly: {anomaly.get('detail', '')}")
        if self.alert_manager:
            self.alert_manager.create_alert(
                title="健康异常",
                detail=anomaly.get("detail", ""),
                severity=anomaly.get("severity", "warning"),
                layer="ops"
            )
        return {"action": "log_and_alert", "status": "logged", "detail": "已记录并告警"}

    def _heal_replay_data(self, anomaly: dict) -> dict:
        pipeline = anomaly.get("pipeline", "")
        logger.info(f"Replaying data for pipeline {pipeline}")
        if self.orchestrator and pipeline:
            try:
                result = self.orchestrator.run_pipeline(pipeline)
                status = "success" if getattr(result, 'status', '') == "success" else "failed"
                return {"action": "replay_data", "target": pipeline, "status": status, "detail": "数据重放完成"}
            except Exception as e:
                return {"action": "replay_data", "target": pipeline, "status": "failed", "error": str(e)}
        return {"action": "replay_data", "target": pipeline, "status": "skipped", "detail": "无 orchestrator"}

    def _heal_reset_baseline(self, anomaly: dict) -> dict:
        metric = anomaly.get("metric", "")
        logger.info(f"Resetting baseline for metric {metric}")
        conn = self.db.get_connection()
        conn.execute(
            "UPDATE metric_baselines SET needs_reset=1 WHERE metric_name=?",
            (metric,)
        )
        conn.commit()
        return {"action": "reset_baseline", "target": metric, "status": "success", "detail": f"基线 {metric} 已标记重置"}

    def _heal_purge_old_data(self, anomaly: dict) -> dict:
        table = anomaly.get("table", "")
        if not table or not _IDENTIFIER_RE.match(table):
            return {"action": "purge_old_data", "target": table, "status": "skipped", "detail": f"无效表名: {table}"}
        logger.info(f"Purging old data from {table}")
        conn = self.db.get_connection()
        conn.execute(
            f"DELETE FROM {table} WHERE created_at < datetime('now', '-30 days', '+8 hours')"
        )
        conn.commit()
        return {"action": "purge_old_data", "target": table, "status": "success", "detail": f"已清理 {table} 30天前的数据"}

    def _heal_failover(self, anomaly: dict) -> dict:
        pipeline = anomaly.get("pipeline", "unknown")
        logger.warning(f"Failover triggered for {pipeline}")
        if self.alert_manager:
            self.alert_manager.create_alert(
                title=f"管道 {pipeline} 故障转移",
                detail=anomaly.get("detail", ""),
                severity="critical",
                layer="ops"
            )
        conn = self.db.get_connection()
        conn.execute(
            "INSERT INTO action_log (action, target, status, detail) VALUES (?, ?, ?, ?)",
            ("failover", pipeline, "initiated", "故障转移已触发")
        )
        conn.commit()
        return {"action": "failover", "target": pipeline, "status": "initiated", "detail": "故障转移已触发"}
