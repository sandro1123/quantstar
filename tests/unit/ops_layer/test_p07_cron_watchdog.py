"""P0-7 方向B: cron 看门狗包装器测试 (scripts/cron_watchdog.sh)。

验证: 超时预算运行时从 ops.yaml 派生 (不硬编码); 超预算 SIGTERM->SIGKILL;
事件写 logs/watchdog.jsonl (铁律 #12)。
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
WATCHDOG = ROOT / "scripts" / "cron_watchdog.sh"


def _yaml(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "ops.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def _run(tmp_path: Path, yaml_path: Path, key: str, *cmd: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["QS_OPS_YAML"] = str(yaml_path)
    env["QS_WATCHDOG_LOG"] = str(tmp_path / "watchdog.jsonl")
    # Q4: RC=124/137 分支会调 lock_probe.py --force — 隔离其全部副作用,
    # 绝不触碰生产 data/qs.db / logs。探针指向临时空库 (BEGIN IMMEDIATE 必成功=healthy)。
    probe_db = tmp_path / "probe.db"
    if not probe_db.exists():
        import sqlite3
        sqlite3.connect(str(probe_db)).close()
    env["QS_LOCK_PROBE_DB"] = str(probe_db)
    env["QS_LOCK_PROBE_STATE"] = str(tmp_path / "lock_probe_state")
    env["QS_LOCK_PROBE_LOG"] = str(tmp_path / "lock_probe.jsonl")
    env["QS_GHOST_REGISTRY"] = str(tmp_path / "ghost_registry.jsonl")
    return subprocess.run(
        ["bash", str(WATCHDOG), key, *cmd],
        capture_output=True, text=True, timeout=30, env=env,
    )


def _events(tmp_path: Path) -> list[dict]:
    log = tmp_path / "watchdog.jsonl"
    if not log.exists():
        return []
    return [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]


@pytest.fixture
def fast_yaml(tmp_path):
    return _yaml(tmp_path, """
ops:
  pipelines:
    fast_pipe:
      timeout_s: 1
  cron_watchdog:
    grace_s: 1
    default_timeout_s: 2
    overrides:
      fast_script: 1
""")


def test_over_budget_killed(fast_yaml, tmp_path):
    r = _run(tmp_path, fast_yaml, "fast_pipe", "sleep", "10")
    assert r.returncode == 124  # timeout(1) 的超时退出码
    ev = _events(tmp_path)
    assert any(e["event"] == "watchdog_killed" and e["key"] == "fast_pipe" for e in ev)
    start = next(e for e in ev if e["event"] == "watchdog_start")
    assert start["budget_s"] == 2  # timeout_s(1) x (0+1) + 30x0 + grace_s(1), 从 yaml 派生


def test_budget_uses_ceiling_formula_with_retries(tmp_path):
    """Q5/DL-65: 管道带 max_retries 时, 预算 = timeout x (retries+1) + delay x retries + grace,
    与 schedule_check.check_budget_coupling 的 ceiling 公式同源 (修预算横跨重试缺陷)。"""
    y = _yaml(tmp_path, """
ops:
  pipelines:
    retry_pipe:
      timeout_s: 10
      max_retries: 2
      retry_delay_s: 5
  cron_watchdog:
    grace_s: 3
    default_timeout_s: 2
""")
    r = _run(tmp_path, y, "retry_pipe", "true")
    assert r.returncode == 0
    start = next(e for e in _events(tmp_path) if e["event"] == "watchdog_start")
    # ceiling = 10 x (2+1) + 5 x 2 + 3 = 30 + 10 + 3 = 43
    assert start["budget_s"] == 43
    assert start["grace_s"] == 3


def test_override_key_keeps_simple_budget(tmp_path):
    """非管道入口 (overrides) 无重试概念, 维持 t+grace (不进 ceiling 公式)。"""
    y = _yaml(tmp_path, """
ops:
  cron_watchdog:
    grace_s: 3
    default_timeout_s: 2
    overrides:
      plain_script: 10
""")
    r = _run(tmp_path, y, "plain_script", "true")
    assert r.returncode == 0
    start = next(e for e in _events(tmp_path) if e["event"] == "watchdog_start")
    assert start["budget_s"] == 13  # 10 + 3, 无重试


def test_within_budget_passes(fast_yaml, tmp_path):
    r = _run(tmp_path, fast_yaml, "fast_pipe", "true")
    assert r.returncode == 0
    ev = _events(tmp_path)
    assert any(e["event"] == "watchdog_done" and e["rc"] == 0 for e in ev)


def test_override_key_for_scripts(fast_yaml, tmp_path):
    r = _run(tmp_path, fast_yaml, "fast_script", "sleep", "10")
    assert r.returncode == 124
    start = next(e for e in _events(tmp_path) if e["event"] == "watchdog_start")
    assert start["budget_s"] == 2  # override(1) + grace(1)


def test_unknown_key_falls_back_to_default(fast_yaml, tmp_path):
    r = _run(tmp_path, fast_yaml, "no_such_pipe", "true")
    assert r.returncode == 0
    start = next(e for e in _events(tmp_path) if e["event"] == "watchdog_start")
    assert start["budget_s"] == 3  # default_timeout_s(2) + grace(1)


def test_exit_code_propagated(fast_yaml, tmp_path):
    r = _run(tmp_path, fast_yaml, "fast_pipe", "bash", "-c", "exit 7")
    assert r.returncode == 7


def test_missing_key_usage_error(tmp_path):
    env = dict(os.environ)
    env["QS_WATCHDOG_LOG"] = str(tmp_path / "watchdog.jsonl")
    r = subprocess.run(["bash", str(WATCHDOG)], capture_output=True, text=True, env=env)
    assert r.returncode == 2
