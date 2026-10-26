# -*- coding: utf-8 -*-
"""#80 metric_baselines 死账本通电 — TDD RED 测试面。

设计稿: vault/10-项目/QuantStar/分层讨论/zombie两表施工设计稿-D-20260901.md §2。
「三重死」定性: 表只有 UPDATE 读者无 INSERT 写者 + AnomalyDetector 基线在
进程内存 dict 从不落库 + heal() F6.3 冻结零调用。

新行为契约 (统筹 GO 七决策点):
  A. refresh_duration_baselines(pipelines, window_days):
     - 14 作业白名单 × pipeline_runs 20 天窗 success duration 样本 → upsert
     - 首跑建行 (INSERT...ON CONFLICT(metric_name) DO UPDATE)
     - 重跑不双写 (id 不增, created_at 不刷, needs_reset 不碰)
     - 样本 < 10 跳过 (宁可 NULL 不可假值)
     - 门控 ops.anomaly_detection.enabled=False → 全跳过
  B. check_duration_baseline:
     - z = (last - mean)/std, |z|>3 → baseline_deviation 告警 dict
     - 正常 z → None 静默
     - std=0 → 除零守护返 None
     - needs_reset=1 (人工停比) → 不比对保旧基线
  C. 迁移: _ensure_metric_baselines_columns 幂等 ALTER ×3
     (baseline_std/sample_count/metric_kind DEFAULT 'pipeline_duration')
  D. orchestrator 维护段: _run_anomaly_detection 内 health_check 16:50 挂点,
     零新增 cron (铁律 #11); 失败 fail-safe 不阻断健康检查主流程
fixture: Database(':memory:') + OPS_TABLES_DDL (test_s6 先例)。
"""
import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from ops_layer.anomaly_detector import AnomalyDetector
from ops_layer.core.tables import OPS_TABLES_DDL


def _db():
    from common.database import Database
    db = Database(":memory:")
    db.get_connection().executescript(OPS_TABLES_DDL)
    return db


def _insert_run(conn, pipeline, duration_s, status="success",
                date=None, run_id=None):
    # W2 (2026-09-20): seed 日期改为相对今天 —— 原硬编码 2026-08-30,
    # 实现按 date('now')-window_days 查询, 09-20 后 seed 掉出 20 天窗口
    # 导致 4 测全红 (时间敏感测试自然过期, 与任何实现改动无关, 判例: 勿用硬日期)。
    if date is None:
        from datetime import datetime, timedelta
        date = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO pipeline_runs (run_id, pipeline_name, date, start_time, status, duration_s) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (run_id or f"{pipeline}-{date}-{duration_s}-{id(object())}", pipeline, date[:10], date, status, duration_s),
    )


def _seed_durations(conn, pipeline, count, base=60.0, spread=2.0):
    for i in range(count):
        # i 序号保 run_id 唯一; spread=0 时全同值 (std=0 用例)
        _insert_run(conn, pipeline, base + (i % 3) * spread,
                    run_id=f"{pipeline}-seed-{i}")


PIPES = ["morning_brief", "health_check"]


class TestRefreshDurationBaselines:
    def test_first_run_inserts_rows(self):
        """首跑 → 每管道一行, 五值齐 (value/std/count/kind/updated_at)。"""
        db = _db()
        conn = db.get_connection()
        _seed_durations(conn, "morning_brief", 12)
        det = AnomalyDetector(db)
        stats = det.refresh_duration_baselines(PIPES, window_days=20)
        rows = conn.execute(
            "SELECT metric_name, baseline_value, baseline_std, sample_count, metric_kind "
            "FROM metric_baselines ORDER BY metric_name").fetchall()
        assert len(rows) == 1, f"morning_brief 应建 1 行 (health_check 无样本跳过), 实际 {len(rows)}"
        r = dict(rows[0])
        assert r["metric_name"] == "morning_brief"
        assert r["baseline_value"] > 0
        assert r["baseline_std"] > 0
        assert r["sample_count"] == 12
        assert r["metric_kind"] == "pipeline_duration"
        assert stats is not None and stats.get("upserted", 0) >= 1

    def test_rerun_no_double_write(self):
        """重跑 → 行数不增, id 不变, created_at 不刷, needs_reset 不碰。"""
        db = _db()
        conn = db.get_connection()
        _seed_durations(conn, "morning_brief", 12)
        det = AnomalyDetector(db)
        det.refresh_duration_baselines(PIPES, window_days=20)
        row0 = dict(conn.execute(
            "SELECT id, created_at, updated_at, needs_reset FROM metric_baselines").fetchone())
        # needs_reset 人为置 1 (停比态), 重跑必须保住
        conn.execute("UPDATE metric_baselines SET needs_reset=1")
        conn.commit()
        det.refresh_duration_baselines(PIPES, window_days=20)
        rows = conn.execute(
            "SELECT id, created_at, needs_reset FROM metric_baselines").fetchall()
        assert len(rows) == 1, "重跑不得双写 (ON CONFLICT DO UPDATE 非 INSERT)"
        assert dict(rows[0])["id"] == row0["id"], "同 metric 行 id 稳定"
        assert dict(rows[0])["created_at"] == row0["created_at"], "created_at 不刷 (首建时间)"
        assert dict(rows[0])["needs_reset"] == 1, "needs_reset 是人工停比位, 维护段不得触碰"

    def test_insufficient_samples_skip(self):
        """样本 < 10 → 跳过不建行 (宁可 NULL 不可假值)。"""
        db = _db()
        conn = db.get_connection()
        _seed_durations(conn, "morning_brief", 5)  # 5 < 10
        det = AnomalyDetector(db)
        stats = det.refresh_duration_baselines(PIPES, window_days=20)
        rows = conn.execute("SELECT COUNT(*) FROM metric_baselines").fetchone()
        assert rows[0] == 0, "样本不足不得建假基线"
        assert stats is not None and stats.get("skipped", 0) >= 1

    def test_gate_disabled_skips_all(self):
        """门控 ops.anomaly_detection.enabled=False → 全跳过零行。"""
        db = _db()
        conn = db.get_connection()
        _seed_durations(conn, "morning_brief", 12)
        det = AnomalyDetector(db)
        stats = det.refresh_duration_baselines(PIPES, window_days=20, enabled=False)
        rows = conn.execute("SELECT COUNT(*) FROM metric_baselines").fetchone()
        assert rows[0] == 0, "门控关 = 整族静默 (同 #79 kill-switch 语义)"
        assert stats is not None and stats.get("skipped", 0) >= 1


class TestCheckDurationBaseline:
    def test_z_above_threshold_alerts(self):
        """|z|>3 → baseline_deviation dict (severity=warning)。"""
        db = _db()
        conn = db.get_connection()
        _seed_durations(conn, "morning_brief", 12, base=60.0, spread=1.0)
        det = AnomalyDetector(db)
        det.refresh_duration_baselines(PIPES, window_days=20)
        out = det.check_duration_baseline("morning_brief", duration_s=500.0)
        assert out is not None, "z 远超 3 应告警"
        assert out["anomaly_type"] == "baseline_deviation"
        assert out["severity"] == "warning"
        assert "morning_brief" in out["detail"]

    def test_z_normal_silent(self):
        """正常范围 → None 静默。"""
        db = _db()
        conn = db.get_connection()
        _seed_durations(conn, "morning_brief", 12, base=60.0, spread=1.0)
        det = AnomalyDetector(db)
        det.refresh_duration_baselines(PIPES, window_days=20)
        assert det.check_duration_baseline("morning_brief", duration_s=61.0) is None

    def test_std_zero_guard(self):
        """std=0 (全同值) → 除零守护返 None 不炸。"""
        db = _db()
        conn = db.get_connection()
        _seed_durations(conn, "morning_brief", 12, base=60.0, spread=0)  # 全 60.0
        det = AnomalyDetector(db)
        det.refresh_duration_baselines(PIPES, window_days=20)
        out = det.check_duration_baseline("morning_brief", duration_s=999.0)
        assert out is None or out.get("anomaly_type") != "baseline_deviation", \
            "std=0 不比对 (无法区分, 宁可静默不可误报)"

    def test_needs_reset_stops_comparison(self):
        """needs_reset=1 (人工停比) → 不比对, 保旧基线。"""
        db = _db()
        conn = db.get_connection()
        _seed_durations(conn, "morning_brief", 12, base=60.0, spread=1.0)
        det = AnomalyDetector(db)
        det.refresh_duration_baselines(PIPES, window_days=20)
        conn.execute("UPDATE metric_baselines SET needs_reset=1")
        conn.commit()
        assert det.check_duration_baseline("morning_brief", duration_s=500.0) is None, \
            "人工停比期间不得出 baseline_deviation"


class TestColumnMigration:
    def test_ensure_columns_idempotent_double_run(self):
        """老表 (无三新列) 补列; 双跑幂等不炸不重列。"""
        from ops_layer.core.tables import _ensure_metric_baselines_columns
        conn = sqlite3.connect(":memory:")
        conn.execute("""CREATE TABLE metric_baselines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            metric_name TEXT NOT NULL UNIQUE,
            baseline_value REAL,
            needs_reset INTEGER DEFAULT 0,
            updated_at TEXT DEFAULT (datetime('now', 'localtime')),
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )""")
        conn.commit()
        _ensure_metric_baselines_columns(conn)
        _ensure_metric_baselines_columns(conn)  # 幂等双跑
        cols = [r[1] for r in conn.execute("PRAGMA table_info(metric_baselines)").fetchall()]
        assert "baseline_std" in cols and "sample_count" in cols and "metric_kind" in cols
        assert cols.count("baseline_std") == 1, "双跑不得重列"
        conn.close()

    def test_ddl_has_new_columns(self):
        """OPS_TABLES_DDL 同步含三新列 (新库直建全列)。"""
        conn = sqlite3.connect(":memory:")
        conn.executescript(OPS_TABLES_DDL)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(metric_baselines)").fetchall()]
        assert "baseline_std" in cols and "sample_count" in cols and "metric_kind" in cols
        conn.close()


class TestOrchestratorWiring:
    def test_maintenance_segment_runs_in_anomaly_detection(self):
        """维护段挂在 _run_anomaly_detection 内 (16:50 health_check 唯一入口, 零新 cron)。"""
        from ops_layer.alert_manager import AlertManager
        from ops_layer.orchestrator import PipelineOrchestrator
        from ops_layer.pipeline_logger import PipelineLogger

        db = _db()
        conn = db.get_connection()
        _seed_durations(conn, "morning_brief", 12)
        with patch.object(AlertManager, "push_feishu", lambda self, *a, **k: True):
            orch = PipelineOrchestrator(db, PipelineLogger(db), AlertManager(db))
            report = SimpleNamespace(overall_status="green", layers=[])
            orch._run_anomaly_detection(report)
        rows = conn.execute(
            "SELECT metric_name FROM metric_baselines WHERE metric_kind='pipeline_duration'").fetchall()
        assert len(rows) >= 1, "维护段应在 anomaly_detection 调用内顺手 upsert 基线"

    def test_maintenance_fail_safe(self):
        """维护段失败 → 只告警不阻断健康检查主流程 (fail-safe 同现有段)。"""
        from ops_layer.alert_manager import AlertManager
        from ops_layer.orchestrator import PipelineOrchestrator
        from ops_layer.pipeline_logger import PipelineLogger

        db = _db()
        with patch.object(AlertManager, "push_feishu", lambda self, *a, **k: True):
            orch = PipelineOrchestrator(db, PipelineLogger(db), AlertManager(db))
            with patch.object(AnomalyDetector, "refresh_duration_baselines",
                              side_effect=Exception("boom-80")):
                report = SimpleNamespace(overall_status="green", layers=[])
                out = orch._run_anomaly_detection(report)  # 不得抛
        assert out == []  # fail-safe 吞掉返回空列表, 主流程活着
