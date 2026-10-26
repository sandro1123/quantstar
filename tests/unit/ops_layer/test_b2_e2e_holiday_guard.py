# -*- coding: utf-8 -*-
"""B2: e2e 入口假日守卫 —— 非交易日不判红。

问题（2026-10-21 统筹）:
    crontab ``30 20 * * 1-5`` 只排周末, 不排节假日。
    10-01/02/05/06/07 这类法定假日仍有 cron 触发 e2e, 跑出来一堆
    「管道无运行记录」的 fail ⇒ **假日假红**, 污染 e2e 历史与告警。

修法:
    ``run()`` 入口先问 ``TradingCalendar.is_trading_day(target_date)``,
    非交易日直接 success-skip 并具名留痕, **不跑任何判据**。

边界:
    - 只影响「该日期是否交易日」这一件事, 不改任何既有判据
    - 交易日一律正常走完整判据（不许误伤）
"""
import importlib.util
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))


def _load_mod():
    path = os.path.join(ROOT, "scripts", "e2e_verification.py")
    spec = importlib.util.spec_from_file_location("e2e_verification_b2", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load_mod()


class _FakeCal:
    def __init__(self, trading):
        self._trading = trading

    def is_trading_day(self, date_str):
        return self._trading


class _FakeApp:
    """最小 OpsLayer 替身: 只提供 db 与 shutdown。"""

    def __init__(self):
        self.db = None
        self.shutdown_called = False

    def shutdown(self):
        self.shutdown_called = True


@pytest.fixture
def patched(monkeypatch):
    """把 OpsLayer / TradingCalendar / 各 check_* 注入为可控替身。"""
    calls = {"checks": 0, "evidences": []}

    def _fake_check_pipelines(app, date, pipes):
        calls["checks"] += 1
        return [{"name": p, "status": "fail", "detail": "无运行记录"}
                for p in pipes]

    def _fake_check_freshness(app, date):
        calls["checks"] += 1
        return []

    def _fake_check_logs(date):
        calls["checks"] += 1
        return []

    def _fake_write(rec):
        calls["evidences"].append(rec)

    monkeypatch.setattr(MOD, "OpsLayer", lambda: _FakeApp(), raising=False)
    monkeypatch.setattr(MOD, "check_pipelines", _fake_check_pipelines, raising=False)
    monkeypatch.setattr(MOD, "check_data_freshness", _fake_check_freshness, raising=False)
    monkeypatch.setattr(MOD, "check_logs", _fake_check_logs, raising=False)
    monkeypatch.setattr(MOD, "_write_evidence", _fake_write, raising=False)
    monkeypatch.setattr(MOD, "_push_notification", lambda *a, **k: None, raising=False)

    def _set_cal(trading):
        monkeypatch.setattr(MOD, "_is_trading_day", lambda d, db=None: trading,
                            raising=False)
    return calls, _set_cal


def test_holiday_skips_without_running_checks(patched):
    """钉(1) 正向: 非交易日 → 不跑任何判据, 退出码 0, 具名留痕。"""
    calls, set_cal = patched
    set_cal(False)

    ec = MOD.run("2026-10-01", "full", notify_disabled=True)

    assert ec == 0, f"非交易日应 success-skip(退出码0), 实得 {ec}"
    assert calls["checks"] == 0, (
        f"非交易日不应跑任何判据, 实际跑了 {calls['checks']} 组")
    assert calls["evidences"], "非交易日必须写留痕"
    rec = calls["evidences"][-1]
    assert rec.get("skipped") is True, f"留痕须显式标 skipped: {rec}"
    assert "非交易日" in str(rec.get("skip_reason", "")), (
        f"留痕须具名原因「非交易日」: {rec}")


def test_trading_day_runs_full_checks(patched):
    """钉(2) 边界: 交易日 → 守卫不误伤, 正常跑完整判据。"""
    calls, set_cal = patched
    set_cal(True)

    MOD.run("2026-10-20", "full", notify_disabled=True)

    assert calls["checks"] >= 3, (
        f"交易日应跑完整判据组(pre/post/freshness/logs), 实得 {calls['checks']}")
    rec = calls["evidences"][-1]
    assert not rec.get("skipped"), f"交易日不应标 skipped: {rec}"


def test_guard_present_at_run_entry():
    """钉(3) 守卫在位: run() 源码里必须真的调用交易日判定。

    (反向守护: 有人把守卫删了, 这条会红。)
    """
    import inspect
    src = inspect.getsource(MOD.run)
    assert "is_trading_day" in src or "_is_trading_day" in src, (
        "run() 入口缺交易日守卫 —— 假日假红会复发")
