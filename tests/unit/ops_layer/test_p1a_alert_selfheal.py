"""P1-a: 告警自愈 — consecutive_failures 解冻 + 陈旧告警按证据收敛。

病灶:
  1. `_run_anomaly_detection` 只检测→告警, AutoHealer 全部 runbook 永不触发。
     生产 alerts 里「异常检测: consecutive_failures」2026-10-26 10:00 创建后
     一直 open (issue #192), 自愈链路是「建了没通电」。
  2. `scripts/cleanup_stale_alerts.py` 是孤儿工具 (无 crontab / 无调用点),
     生产 15 条 open 里最早可追到 09-01 (71 天未解)。

修法边界 (与 D-30 冻结面显式划界):
  - 只解冻 consecutive_failures 一族 (非破坏性 failover);
  - switch_source / purge_old_data 等破坏性 runbook 仍不自动触发;
  - 陈旧告警只在**有恢复证据**时才关, 不做「超时就关」(判例 #9)。
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from common.database import Database
from ops_layer.auto_healer import AutoHealer
from ops_layer.core.tables import OPS_TABLES_DDL
from ops_layer.orchestrator import PipelineOrchestrator


class _StubAlertManager:
    """最小 AlertManager 替身。

    注意: AutoHealer._heal_failover 走的是 create_alert (不是 send) —
    两者都要提供, 否则自愈会在告警这步抛 AttributeError 被 fail-safe 吞掉,
    表面上「自愈跑了」实际 action_log 都没写 (假绿)。
    """

    def __init__(self):
        self.sent = []

    def send(self, alert):
        self.sent.append(alert)
        return True

    def create_alert(self, title, detail, severity="warning", layer="ops"):
        self.sent.append({"title": title, "detail": detail,
                          "severity": severity, "layer": layer})
        return True


def _mk_orch(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.init_db()
    db.get_connection().executescript(OPS_TABLES_DDL)
    db.get_connection().commit()
    am = _StubAlertManager()
    orch = PipelineOrchestrator(db, None, am, None)
    return orch, db, am


class TestAutonomousHealDispatch:
    def test_consecutive_failures_triggers_heal(self, tmp_path):
        """consecutive_failures 异常触发自愈 (解冻 #192)。"""
        orch, db, am = _mk_orch(tmp_path)
        res = orch._dispatch_autonomous_heal([
            {"anomaly_type": "consecutive_failures", "pipeline": "daily_data_pipeline",
             "detail": "最近连续失败 5 次", "severity": "critical"},
        ])
        assert len(res) == 1
        assert res[0]["action"] == "failover"
        assert res[0]["target"] == "daily_data_pipeline"
        # action_log 落库为证
        row = db.get_connection().execute(
            "SELECT action, target, status FROM action_log WHERE action='failover'").fetchone()
        assert row is not None and row[1] == "daily_data_pipeline"

    def test_switch_source_not_auto_healed(self, tmp_path):
        """switch_source / data_source_down 仍保持 D-30 冻结, 不自动触发。"""
        orch, db, _ = _mk_orch(tmp_path)
        res = orch._dispatch_autonomous_heal([
            {"anomaly_type": "data_source_down", "source": "akshare"},
            {"anomaly_type": "source_stale", "source": "tushare"},
            {"anomaly_type": "data_volume", "table": "daily_klines"},
        ])
        assert res == []
        n = db.get_connection().execute(
            "SELECT COUNT(*) FROM action_log WHERE action IN "
            "('switch_source','purge_old_data')").fetchone()[0]
        assert n == 0

    def test_health_red_not_auto_healed(self, tmp_path):
        """health_red 属观测面, 不在自愈白名单 (避免掩盖真故障)。"""
        orch, _, _ = _mk_orch(tmp_path)
        assert orch._dispatch_autonomous_heal(
            [{"anomaly_type": "health_red", "layer": "overall"}]) == []

    def test_heal_failure_does_not_raise(self, tmp_path, monkeypatch):
        """单条自愈抛错只记录, 不影响其余条目 (fail-safe)。"""
        orch, _, _ = _mk_orch(tmp_path)
        calls = {"n": 0}

        def _boom(self, anomaly):
            calls["n"] += 1
            raise RuntimeError("heal boom")

        monkeypatch.setattr(AutoHealer, "_heal_failover", _boom)
        res = orch._dispatch_autonomous_heal([
            {"anomaly_type": "consecutive_failures", "pipeline": "a"},
            {"anomaly_type": "consecutive_failures", "pipeline": "b"},
        ])
        assert calls["n"] == 2          # 两条都尝试过
        assert all(r["status"] == "error" for r in res)

    def test_empty_anomalies_noop(self, tmp_path):
        orch, _, _ = _mk_orch(tmp_path)
        assert orch._dispatch_autonomous_heal([]) == []


class TestStaleAlertReap:
    def _add_alert(self, db, title, detail, created_at, status="open"):
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO alerts (severity, layer, title, detail, action, status, created_at) "
            "VALUES ('warning','ops',?,?,NULL,?,?)",
            (title, detail, status, created_at))
        conn.commit()
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def _add_run(self, db, name, status, start_time):
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO pipeline_runs (run_id, pipeline_name, date, start_time, status) "
            "VALUES (?,?,?,?,?)",
            (f"r-{name}-{start_time}", name, start_time[:10], start_time, status))
        conn.commit()

    def test_reap_closes_alert_with_recovery_evidence(self, tmp_path):
        """管道在告警后才成功过 → 关闭并留恢复证据。"""
        orch, db, _ = _mk_orch(tmp_path)
        old = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        aid = self._add_alert(db, "管道失败: daily_data_pipeline", "x", old)
        self._add_run(db, "daily_data_pipeline", "success",
                      (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"))
        n = orch._reap_resolved_stale_alerts()
        assert n == 1
        row = db.get_connection().execute(
            "SELECT status, action FROM alerts WHERE id=?", (aid,)).fetchone()
        assert row[0] == "resolved"
        assert "已恢复成功" in (row[1] or "")

    def test_no_recovery_evidence_keeps_alert_open(self, tmp_path):
        """管道告警后从未成功 → 保持 open (不做超时一刀切)。"""
        orch, db, _ = _mk_orch(tmp_path)
        old = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        aid = self._add_alert(db, "管道失败: daily_data_pipeline", "x", old)
        self._add_run(db, "daily_data_pipeline", "failed",
                      (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"))
        assert orch._reap_resolved_stale_alerts() == 0
        row = db.get_connection().execute(
            "SELECT status FROM alerts WHERE id=?", (aid,)).fetchone()
        assert row[0] == "open"

    def test_fresh_alert_not_touched(self, tmp_path):
        """未满 min_age_days 的告警不动 (可能仍在发生中)。"""
        orch, db, _ = _mk_orch(tmp_path)
        recent = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        aid = self._add_alert(db, "管道失败: daily_data_pipeline", "x", recent)
        self._add_run(db, "daily_data_pipeline", "success",
                      datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        assert orch._reap_resolved_stale_alerts() == 0
        row = db.get_connection().execute(
            "SELECT status FROM alerts WHERE id=?", (aid,)).fetchone()
        assert row[0] == "open"

    def test_unrecognized_alert_kept_open(self, tmp_path):
        """认不出宿主管道的告警不关 (宁可留着让人看)。"""
        orch, db, _ = _mk_orch(tmp_path)
        old = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        aid = self._add_alert(db, "某种说不清的告警", "无管道名", old)
        assert orch._reap_resolved_stale_alerts() == 0
        row = db.get_connection().execute(
            "SELECT status FROM alerts WHERE id=?", (aid,)).fetchone()
        assert row[0] == "open"

    def test_resolved_alerts_untouched(self, tmp_path):
        """已 resolved 的告警不重复处理。"""
        orch, db, _ = _mk_orch(tmp_path)
        old = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        aid = self._add_alert(db, "管道失败: daily_data_pipeline", "x", old, status="resolved")
        self._add_run(db, "daily_data_pipeline", "success",
                      datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        assert orch._reap_resolved_stale_alerts() == 0
        row = db.get_connection().execute(
            "SELECT status FROM alerts WHERE id=?", (aid,)).fetchone()
        assert row[0] == "resolved"
