"""P0-3③: chronic 降噪包装器 — 成功静默/失败吐输出/退出码透传"""
import subprocess
from pathlib import Path

import pytest

CHRONIC = Path(__file__).resolve().parents[3] / "scripts" / "chronic.sh"


def _run(*args):
    return subprocess.run(["bash", str(CHRONIC), *args],
                          capture_output=True, text=True, timeout=30)


def test_success_is_silent():
    """成功: 输出被吞, rc=0"""
    r = _run("bash", "-c", "echo noise; echo more >&2")
    assert r.returncode == 0
    assert r.stdout == ""
    assert r.stderr == ""


def test_failure_emits_output():
    """失败: 缓冲的输出全部吐出 (stdout+stderr), rc 透传"""
    r = _run("bash", "-c", "echo boom; echo err >&2; exit 3")
    assert r.returncode == 3
    assert "boom" in r.stdout
    assert "err" in r.stdout  # stderr 也进缓冲, 失败时一并可见


def test_exit_code_passthrough_124():
    """超时语义退出码 (124) 原样透传 — 不得吞码"""
    r = _run("bash", "-c", "exit 124")
    assert r.returncode == 124


def test_no_args_usage_error():
    """无命令 → usage + rc=2"""
    r = _run()
    assert r.returncode == 2
    assert "usage" in r.stderr


def test_temp_file_cleaned():
    """临时缓冲文件用后即删 (trap EXIT)"""
    import glob
    before = set(glob.glob("/tmp/tmp.*"))
    _run("bash", "-c", "echo x; exit 1")
    after = set(glob.glob("/tmp/tmp.*"))
    assert after <= before, "chronic 泄漏临时文件"


def test_watchdog_stack_success_silent():
    """叠加实测: chronic 包 cron_watchdog, 成功路径静默 (jsonl 留痕不受影响)"""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        yaml_p = Path(td) / "ops.yaml"
        yaml_p.write_text(
            "ops:\n  cron_watchdog:\n    grace_s: 5\n"
            "    default_timeout_s: 30\n    overrides: {t: 30}\n",
            encoding="utf-8")
        log_p = Path(td) / "watchdog.jsonl"
        root = Path(__file__).resolve().parents[3]
        wd = root / "scripts" / "cron_watchdog.sh"
        env = {"QS_OPS_YAML": str(yaml_p), "QS_WATCHDOG_LOG": str(log_p),
               "PATH": "/usr/bin:/bin"}
        r = subprocess.run(
            ["bash", str(CHRONIC), "bash", str(wd), "t", "true"],
            capture_output=True, text=True, env=env, timeout=30)
        assert r.returncode == 0
        assert r.stdout == "", "成功路径应静默"
        assert log_p.exists() and "watchdog_done" in log_p.read_text(), \
            "结构化留痕 (jsonl) 不受 chronic 影响"
