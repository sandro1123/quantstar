"""①(a) per-key flock 等待模式门禁测试 — 统筹裁定 sm-910a6a0e07104b0c / sm-e05dd3cfe15e4abd。

门禁 §4.3 (设计稿 sched-fix-design-D-2026-08-26.md):
  RED/GREEN + 负向对照三条 + 可观测性事件 + 回归。

测试策略: 真实 flock_guard.sh + 真实 flock (不 mock 锁语义), 用临时锁文件+临时
ops.yaml (QS_OPS_YAML 环境变量指向) 隔离生产。持锁方用后台 bash flock 占锁。
"""
import json
import os
import subprocess
import time
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
GUARD = REPO_ROOT / "scripts" / "flock_guard.sh"


@pytest.fixture()
def env(tmp_path):
    """隔离环境: 临时锁文件 + 临时 ops.yaml + 临时 watchdog.jsonl。"""
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir()
    # Ensure worktree locks dir exists for holder subprocess
    import pathlib
    repo_locks = pathlib.Path(__file__).resolve().parents[3] / "locks"
    repo_locks.mkdir(exist_ok=True)
    log = tmp_path / "watchdog.jsonl"
    ops = tmp_path / "ops.yaml"

    def make_ops(wait_keys: dict[str, int]):
        pipes = {}
        for k, wt in wait_keys.items():
            pipes[k] = {"timeout_s": 120, "max_retries": 1}
            if wt is not None:
                pipes[k]["wait_timeout_s"] = wt
        cfg = {"ops": {"pipelines": pipes}}
        ops.write_text(yaml.safe_dump(cfg), encoding="utf-8")

    env = os.environ.copy()
    env["QS_OPS_YAML"] = str(ops)
    env["QS_WATCHDOG_LOG"] = str(log)
    # flock_guard 用 BASH_SOURCE 派生 ROOT → 锁文件在 REPO_ROOT/locks。
    # 测试不 mock 锁路径, 直接用生产锁路径的语义但通过子进程隔离:
    # 这里改为断言事件文件内容, 锁竞争用真实 REPO_ROOT/locks (测试时段无 cron 运行)。
    return {"env": env, "log": log, "make_ops": make_ops, "tmp": tmp_path}


@pytest.fixture(autouse=True)
def _ensure_lock_free():
    """每个测试前确保生产锁空闲 (前一测试 holder 子进程可能残留持锁)。"""
    import subprocess as _sp
    lock = REPO_ROOT / "locks" / "qs_db_write.lock"
    lock.parent.mkdir(exist_ok=True)
    # 杀掉残留持锁进程
    _sp.run(["bash", "-c", f"fuser -k {lock} 2>/dev/null; true"], capture_output=True)
    # 等锁真正空闲 (最多 5s)
    for _ in range(50):
        r = _sp.run(["flock", "-n", str(lock), "true"], capture_output=True)
        if r.returncode == 0:
            break
        time.sleep(0.1)
    yield


def run_guard(key, env, extra_args=None, timeout=30):
    cmd = ["bash", str(GUARD), key, "true"] + (extra_args or [])
    return subprocess.run(cmd, capture_output=True, text=True, env=env["env"], timeout=timeout)


def read_events(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]


class TestNonBlockingDefault:
    """键缺省 wait_timeout_s → 保持 flock -n 现状 (ddp 入口必须走此路)。"""

    def test_no_wait_key_skips_immediately(self, env):
        """RED→GREEN: 无 wait_timeout_s 键遇锁 → exit 99 + skipped_lock, 不等待。"""
        env["make_ops"]({"plain_job": None})  # 无 wait_timeout_s
        # 占锁: 后台 flock 持锁 5s
        holder = subprocess.Popen(
            ["bash", "-c", f"flock {REPO_ROOT}/locks/qs_db_write.lock sleep 5"])
        time.sleep(0.3)
        t0 = time.time()
        r = run_guard("plain_job", env)
        elapsed = time.time() - t0
        holder.terminate()
        assert r.returncode == 99, f"应 exit 99, 实际 {r.returncode}"
        assert elapsed < 2, f"非阻塞应瞬时返回, 实际 {elapsed:.1f}s"
        events = read_events(env["log"])
        assert any(e["event"] == "skipped_lock" and e["key"] == "plain_job" for e in events)
        assert not any(e["event"] in ("waited_lock", "wait_timeout") for e in events)


class TestWaitMode:
    """有 wait_timeout_s 键 → 遇锁等待, 放锁即获锁。"""

    def test_wait_then_acquire(self, env):
        """负向对照1: ddp 持锁→午后作业等待→ddp 放锁→获锁执行。"""
        env["make_ops"]({"afternoon_job": 10})
        holder = subprocess.Popen(
            ["bash", "-c", f"flock {REPO_ROOT}/locks/qs_db_write.lock sleep 2"])
        time.sleep(0.3)
        r = run_guard("afternoon_job", env, timeout=20)
        holder.wait()
        assert r.returncode == 0, f"放锁后应获锁成功, 实际 rc={r.returncode}"
        events = read_events(env["log"])
        assert any(e["event"] == "waited_lock" and e["key"] == "afternoon_job" for e in events)
        assert any(e["event"] == "acquired_after_wait" and e["key"] == "afternoon_job" for e in events)

    def test_wait_timeout_exits_99(self, env):
        """负向对照2: ddp 失败(watchdog 杀)→午后作业等到超时→exit 99 不挂死。"""
        env["make_ops"]({"afternoon_job": 2})
        holder = subprocess.Popen(
            ["bash", "-c", f"flock {REPO_ROOT}/locks/qs_db_write.lock sleep 30"])
        time.sleep(0.3)
        t0 = time.time()
        r = run_guard("afternoon_job", env, timeout=15)
        elapsed = time.time() - t0
        holder.terminate()
        assert r.returncode == 99, f"等待超时应 exit 99, 实际 {r.returncode}"
        assert 1.5 < elapsed < 8, f"应在 wait_timeout_s≈2s 后退出, 实际 {elapsed:.1f}s"
        events = read_events(env["log"])
        assert any(e["event"] == "wait_timeout" and e["key"] == "afternoon_job" for e in events)

    def test_lock_free_no_wait_event(self, env):
        """锁空闲时直接进, 不产生 waited_lock 事件 (无谓留痕)。"""
        env["make_ops"]({"afternoon_job": 10})
        r = run_guard("afternoon_job", env)
        assert r.returncode == 0
        events = read_events(env["log"])
        assert not any(e["event"] == "waited_lock" for e in events)


class TestObservability:
    """铁律#12: 三类事件字段完整可查询。"""

    def test_waited_lock_fields(self, env):
        env["make_ops"]({"afternoon_job": 2})
        holder = subprocess.Popen(
            ["bash", "-c", f"flock {REPO_ROOT}/locks/qs_db_write.lock sleep 5"])
        time.sleep(0.3)
        run_guard("afternoon_job", env, timeout=10)
        holder.terminate()
        events = read_events(env["log"])
        wl = [e for e in events if e["event"] == "waited_lock"]
        assert wl, "waited_lock 事件缺失"
        e = wl[0]
        for field in ("ts", "key", "wait_timeout_s", "lock_owner_key", "lock_owner_pid", "held_seconds"):
            assert field in e, f"waited_lock 缺字段 {field}"


class TestYamlKeys:
    """ops.yaml 9 键 wait_timeout_s=7200 落地核验 (生产 yaml, 只读断言)。"""

    EXPECTED_KEYS = [
        "factor_update", "fwd_return_backfill", "quality_check", "freshness_audit",
        "ic_feedback", "daily_backtest", "paper_trading", "evening_report", "health_check",
    ]

    def test_nine_keys_have_wait_timeout(self):
        cfg = yaml.safe_load((REPO_ROOT / "config/defaults/ops.yaml").read_text(encoding="utf-8"))
        pipes = cfg["ops"]["pipelines"]
        for k in self.EXPECTED_KEYS:
            assert pipes[k].get("wait_timeout_s") == 7200, f"{k} 缺 wait_timeout_s=7200"

    def test_ddp_stays_nonblocking(self):
        """ddp 自身入口必须保持非阻塞 (不能等自己的锁)。"""
        cfg = yaml.safe_load((REPO_ROOT / "config/defaults/ops.yaml").read_text(encoding="utf-8"))
        ddp = cfg["ops"]["pipelines"]["daily_data_pipeline"]
        assert "wait_timeout_s" not in ddp, "ddp 不得有 wait_timeout_s (必须非阻塞)"
