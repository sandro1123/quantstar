"""P0-7 方向C: ghost 登记 + 有界 shutdown + critical 告警。

红线回归守卫:
- shutdown 不改变"不掐断 ghost"语义 — 只做有界等待, 超时后仍 shutdown(wait=False, cancel_futures=True)
- 超时路径必须登记 ghost 并发 critical 告警 (铁律 #12 可观测不可静默)
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

from common.database import Database
from ops_layer.alert_manager import AlertManager
from ops_layer.ghost_registry import GhostRegistry
from ops_layer.orchestrator import PipelineOrchestrator
from ops_layer.pipeline_logger import PipelineLogger


def _orchestrator():
    db = Database(":memory:")
    db.init_db()
    from ops_layer.core.tables import OPS_TABLES_DDL

    conn = db.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.commit()
    return db, PipelineOrchestrator(db, PipelineLogger(db), AlertManager(db))


class TestGhostRegistry:
    def test_register_emits_jsonl_event(self, tmp_path, monkeypatch):
        path = tmp_path / "ghosts.jsonl"
        monkeypatch.setenv("QS_GHOST_REGISTRY", str(path))
        reg = GhostRegistry()
        pool = ThreadPoolExecutor(max_workers=1)
        fut = pool.submit(time.sleep, 0.01)
        rec = reg.register("daily_data_pipeline", fut, 2400, run_id="r1")
        assert rec.pipeline_name == "daily_data_pipeline"
        event = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        assert event["event"] == "ghost_registered"
        assert event["pipeline"] == "daily_data_pipeline"
        assert event["timeout_s"] == 2400
        pool.shutdown(wait=True)

    def test_active_excludes_finished(self):
        reg = GhostRegistry()
        pool = ThreadPoolExecutor(max_workers=1)
        fut = pool.submit(lambda: None)
        reg.register("p", fut, 1)
        deadline = time.time() + 2
        while not fut.done() and time.time() < deadline:
            time.sleep(0.01)
        assert reg.active() == []
        assert len(reg.snapshot()) == 1
        assert reg.snapshot()[0]["alive"] is False
        pool.shutdown(wait=True)

    def test_wait_active_returns_empty_when_finished(self):
        reg = GhostRegistry()
        pool = ThreadPoolExecutor(max_workers=1)
        reg.register("p", pool.submit(time.sleep, 0.05), 1)
        assert reg.wait_active(5.0) == []
        pool.shutdown(wait=True)

    def test_wait_active_returns_survivors(self):
        reg = GhostRegistry()
        pool = ThreadPoolExecutor(max_workers=1)
        reg.register("p", pool.submit(time.sleep, 5.0), 1)
        survivors = reg.wait_active(0.2)
        assert len(survivors) == 1
        assert survivors[0].pipeline_name == "p"
        pool.shutdown(wait=False, cancel_futures=True)


class TestOrchestratorGhostWiring:
    def test_timeout_registers_ghost_and_critical_alert(self, monkeypatch):
        db, orch = _orchestrator()
        orch._pipeline_configs = {"hang_pipe": {"max_retries": 0, "timeout_s": 1}}
        sent = []
        orch.alert_manager.send = MagicMock(side_effect=lambda a: sent.append(a) or True)
        monkeypatch.setattr(orch, "_execute_pipeline", lambda name, cfg: time.sleep(3))

        result = orch.run_pipeline("hang_pipe")

        assert result.status == "failed"
        assert "timeout" in result.error
        ghosts = orch.ghosts.snapshot()
        assert len(ghosts) == 1
        assert ghosts[0]["pipeline"] == "hang_pipe"
        assert any(a.severity == "critical" and "Ghost" in a.title for a in sent)
        orch.ghosts.wait_active(10)  # 等 ghost 线程自然退出, 避免泄漏到其他测试
        db.close()

    def test_shutdown_bounded_wait_alerts_on_survivors(self):
        db, orch = _orchestrator()
        pool = ThreadPoolExecutor(max_workers=1)
        orch.ghosts.register("slow_pipe", pool.submit(time.sleep, 5.0), 1)
        sent = []
        orch.alert_manager.send = MagicMock(side_effect=lambda a: sent.append(a) or True)

        orch.shutdown(timeout=0.2)

        assert any(a.severity == "critical" and "shutdown" in a.title for a in sent)
        pool.shutdown(wait=False, cancel_futures=True)
        db.close()

    def test_shutdown_without_ghosts_sends_no_alert(self):
        db, orch = _orchestrator()
        sent = []
        orch.alert_manager.send = MagicMock(side_effect=lambda a: sent.append(a) or True)
        orch.shutdown(timeout=0.1)
        assert sent == []
        db.close()
