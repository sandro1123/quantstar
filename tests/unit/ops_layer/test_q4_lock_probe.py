"""Q4: BEGIN IMMEDIATE 锁健康探针测试 (src/ops_layer/lock_probe.py + scripts/lock_probe.py)。

覆盖:
- 三分支: healthy(锁可获取) / locked(写锁被占) / degraded(探针自身异常 ro 降级)
- 三约束: 短 busy_timeout / try-finally rollback(探针绝不残留持锁) / 频率节流+--force 绕过
- ghost 闭环: resolve() 标记已结束 ghost; emit_lock_released 跨进程 jsonl 标记
- watchdog 接线: RC=124 后写 watchdog_lock_probe 事件 (隔离, 不碰生产库)

红线: 全部用 tmp_path 临时库, 绝不触碰生产 data/qs.db。
"""

import json
import os
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ops_layer.lock_probe import probe
from ops_layer.ghost_registry import GhostRegistry, emit_lock_released

ROOT = Path(__file__).resolve().parents[3]
CLI = ROOT / "scripts" / "lock_probe.py"


@pytest.fixture
def tmp_db(tmp_path):
    p = tmp_path / "t.db"
    sqlite3.connect(str(p)).close()
    return str(p)


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    """探针节流状态文件隔离到 tmp, 避免测试间互相节流。"""
    monkeypatch.setenv("QS_LOCK_PROBE_STATE", str(tmp_path / "state"))


class TestProbeBranches:
    def test_healthy_on_free_db(self, tmp_db):
        r = probe(tmp_db, force=True)
        assert r["status"] == "healthy"
        assert "BEGIN IMMEDIATE" in r["detail"]

    def test_locked_when_write_lock_held(self, tmp_db):
        # 另一连接持写锁 (BEGIN IMMEDIATE 未提交)
        holder = sqlite3.connect(tmp_db, timeout=5)
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("CREATE TABLE IF NOT EXISTS t(x)")
        try:
            r = probe(tmp_db, busy_timeout_ms=200, force=True)
            assert r["status"] == "locked"
            assert "locked" in r["detail"].lower() or "busy" in r["detail"].lower()
        finally:
            holder.rollback()
            holder.close()

    def test_probe_leaves_no_lock_behind(self, tmp_db):
        """约束2: 探针 healthy 后, 写锁必须已释放 — 另一连接能立刻 BEGIN IMMEDIATE。"""
        r = probe(tmp_db, force=True)
        assert r["status"] == "healthy"
        c = sqlite3.connect(tmp_db, timeout=1)
        c.execute("BEGIN IMMEDIATE")  # 不应阻塞/撞锁
        c.rollback()
        c.close()

    def test_probe_leaves_no_lock_after_locked_branch(self, tmp_db):
        """约束2: locked 分支后探针也不残留持锁 (rollback+close 在 finally)。"""
        holder = sqlite3.connect(tmp_db, timeout=5)
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("CREATE TABLE IF NOT EXISTS t(x)")
        try:
            probe(tmp_db, busy_timeout_ms=100, force=True)
        finally:
            holder.rollback()
            holder.close()
        # 持锁者释放后, 探针应能 healthy (证明它自己没残留锁)
        r = probe(tmp_db, force=True)
        assert r["status"] == "healthy"

    def test_degraded_on_missing_db_path(self, tmp_path):
        """非 locked 异常 (库文件不存在/不可打开) → ro 降级, 不误报 locked。"""
        r = probe(str(tmp_path / "no_such_dir" / "x.db"), force=True)
        assert r["status"] == "degraded"
        assert r["status"] != "locked"


class TestThrottle:
    def test_second_call_within_interval_throttled(self, tmp_db):
        r1 = probe(tmp_db, min_interval_s=60, force=False)
        assert r1["status"] == "healthy"
        r2 = probe(tmp_db, min_interval_s=60, force=False)
        assert r2["status"] == "throttled"

    def test_force_bypasses_throttle(self, tmp_db):
        probe(tmp_db, min_interval_s=60, force=False)
        r = probe(tmp_db, min_interval_s=60, force=True)
        assert r["status"] == "healthy"  # force 不被节流


class TestGhostClosure:
    def test_resolve_marks_dead_ghosts(self):
        reg = GhostRegistry()
        pool = ThreadPoolExecutor(max_workers=1)
        fut = pool.submit(lambda: None)
        reg.register("p", fut, 1)
        deadline = time.time() + 2
        while not fut.done() and time.time() < deadline:
            time.sleep(0.01)
        resolved = reg.resolve()
        assert len(resolved) == 1
        assert reg.snapshot()[0]["resolved"] is True
        pool.shutdown(wait=True)

    def test_resolve_skips_alive_ghosts(self):
        reg = GhostRegistry()
        pool = ThreadPoolExecutor(max_workers=1)
        reg.register("p", pool.submit(time.sleep, 5.0), 1)
        assert reg.resolve() == []  # 仍存活, 不解析
        assert reg.snapshot()[0]["resolved"] is False
        pool.shutdown(wait=False, cancel_futures=True)

    def test_resolve_filters_by_pipeline(self):
        reg = GhostRegistry()
        pool = ThreadPoolExecutor(max_workers=2)
        f1 = pool.submit(lambda: None)
        f2 = pool.submit(lambda: None)
        reg.register("a", f1, 1)
        reg.register("b", f2, 1)
        time.sleep(0.1)
        resolved = reg.resolve(pipeline_name="a")
        assert len(resolved) == 1
        assert resolved[0].pipeline_name == "a"
        pool.shutdown(wait=True)

    def test_emit_lock_released_writes_jsonl(self, tmp_path, monkeypatch):
        path = tmp_path / "ghosts.jsonl"
        monkeypatch.setenv("QS_GHOST_REGISTRY", str(path))
        emit_lock_released("test marker")
        event = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        assert event["event"] == "ghost_lock_released"
        assert "test marker" in event["note"]


class TestCLI:
    def _run_cli(self, tmp_path, db, *extra):
        env = dict(os.environ)
        env["QS_LOCK_PROBE_DB"] = db
        env["QS_LOCK_PROBE_STATE"] = str(tmp_path / "state")
        env["QS_LOCK_PROBE_LOG"] = str(tmp_path / "lock_probe.jsonl")
        env["QS_GHOST_REGISTRY"] = str(tmp_path / "ghosts.jsonl")
        env["PYTHONPATH"] = str(ROOT / "src")
        return subprocess.run(
            [sys.executable, str(CLI), *extra],
            capture_output=True, text=True, timeout=30, env=env,
        )

    def test_cli_healthy_exit_0(self, tmp_path, tmp_db):
        r = self._run_cli(tmp_path, tmp_db, "--force")
        assert r.returncode == 0
        events = [json.loads(l) for l in
                  (tmp_path / "lock_probe.jsonl").read_text().splitlines()]
        assert any(e["status"] == "healthy" for e in events)

    def test_cli_locked_exit_3(self, tmp_path, tmp_db):
        holder = sqlite3.connect(tmp_db, timeout=5)
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("CREATE TABLE IF NOT EXISTS t(x)")
        try:
            r = self._run_cli(tmp_path, tmp_db, "--force")
            assert r.returncode == 3
        finally:
            holder.rollback()
            holder.close()

    def test_cli_healthy_writes_ghost_lock_released(self, tmp_path, tmp_db):
        self._run_cli(tmp_path, tmp_db, "--force")
        ghosts = tmp_path / "ghosts.jsonl"
        assert ghosts.exists()
        events = [json.loads(l) for l in ghosts.read_text().splitlines()]
        assert any(e["event"] == "ghost_lock_released" for e in events)


class TestWatchdogWiring:
    """RC=124 分支调 lock_probe --force 并写 watchdog_lock_probe 事件 (隔离)。"""

    def test_kill_branch_emits_lock_probe_event(self, tmp_path):
        yaml_path = tmp_path / "ops.yaml"
        yaml_path.write_text(
            "ops:\n  pipelines:\n    p:\n      timeout_s: 1\n"
            "  cron_watchdog:\n    grace_s: 1\n    default_timeout_s: 2\n",
            encoding="utf-8")
        probe_db = tmp_path / "probe.db"
        sqlite3.connect(str(probe_db)).close()
        env = dict(os.environ)
        env["QS_OPS_YAML"] = str(yaml_path)
        env["QS_WATCHDOG_LOG"] = str(tmp_path / "watchdog.jsonl")
        env["QS_LOCK_PROBE_DB"] = str(probe_db)
        env["QS_LOCK_PROBE_STATE"] = str(tmp_path / "lp_state")
        env["QS_LOCK_PROBE_LOG"] = str(tmp_path / "lp.jsonl")
        env["QS_GHOST_REGISTRY"] = str(tmp_path / "ghosts.jsonl")
        r = subprocess.run(
            ["bash", str(ROOT / "scripts" / "cron_watchdog.sh"), "p", "sleep", "10"],
            capture_output=True, text=True, timeout=30, env=env)
        assert r.returncode == 124
        events = [json.loads(l) for l in
                  (tmp_path / "watchdog.jsonl").read_text().splitlines()]
        lp = [e for e in events if e["event"] == "watchdog_lock_probe"]
        assert lp, "RC=124 分支必须写 watchdog_lock_probe 事件"
        assert lp[0]["probe_rc"] == 0  # 临时空库 => healthy
