"""OPS-04: SQLite 锁竞争治理 (ops 侧自救) — retry_on_locked 行为边界

08-20 事故: 崩溃落库路径二次撞锁双重失败。测 retry 语义:
- locked 前 N-1 次失败、第 N 次成功 → 返回结果
- locked 3 次耗尽 → 放弃返回 None 且不抛
- 非 locked 的 OperationalError → 原样抛出 (不吞真错误)
- 正常路径 → 直接返回不重试
"""

import sqlite3
from unittest.mock import MagicMock

from ops_layer.run_guard import retry_on_locked


def test_locked_then_success(monkeypatch):
    """前 2 次 locked, 第 3 次成功 → 返回结果。"""
    monkeypatch.setattr("ops_layer.run_guard.time.sleep", lambda s: None)
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    assert retry_on_locked(fn, attempts=3, delay_s=0) == "ok"
    assert calls["n"] == 3


def test_locked_exhausted_returns_none_without_raising(monkeypatch):
    """3 次全 locked → 放弃返回 None, 不抛 (fail-safe)。"""
    monkeypatch.setattr("ops_layer.run_guard.time.sleep", lambda s: None)
    fn = MagicMock(side_effect=sqlite3.OperationalError("database is locked"))

    result = retry_on_locked(fn, attempts=3, delay_s=0)

    assert result is None
    assert fn.call_count == 3


def test_non_locked_operational_error_propagates(monkeypatch):
    """非 locked 错误 (如 no such table) 原样抛出, 不被 retry 吞掉。"""
    monkeypatch.setattr("ops_layer.run_guard.time.sleep", lambda s: None)
    fn = MagicMock(side_effect=sqlite3.OperationalError("no such table: pipeline_runs"))

    try:
        retry_on_locked(fn, attempts=3, delay_s=0)
        assert False, "应当抛出"
    except sqlite3.OperationalError as e:
        assert "no such table" in str(e)
    assert fn.call_count == 1  # 不重试


def test_success_no_retry():
    """正常路径直接返回, 不触发重试。"""
    fn = MagicMock(return_value=42)

    assert retry_on_locked(fn, attempts=3, delay_s=0) == 42
    assert fn.call_count == 1


def test_sleep_called_between_retries(monkeypatch):
    """重试间隔确实等待 delay_s (行为而非实现: 验证 sleep 被调用)。"""
    sleeps = []
    monkeypatch.setattr("ops_layer.run_guard.time.sleep", lambda s: sleeps.append(s))
    fn = MagicMock(side_effect=sqlite3.OperationalError("database is locked"))

    retry_on_locked(fn, attempts=3, delay_s=2.0)

    assert sleeps == [2.0, 2.0]  # 3 次尝试之间有 2 次等待
