# -*- coding: utf-8 -*-
"""P1-1 注册面对账 (heartbeat_check, 09-28)。

缺陷 (09-25 已谳): cron_watchdog.overrides 键若不在 ops.pipelines, budget 派生
if key in pipelines 分支翻转, 裸加靠 default 巧合=300 — 两注册面漂移无显形。
修法: orphan_override_keys 纯函数 + check() 启动 warning 列孤儿键。
"""
import logging
from datetime import datetime


def _orphan(overrides, pipelines):
    from ops_layer.heartbeat_check import orphan_override_keys
    return orphan_override_keys(overrides, pipelines)


# 钉①: overrides 有 pipelines 无 → 返回孤儿键
def test_orphan_detected():
    got = _orphan({"daily_update": 7200, "ghost_leg": 300},
                  {"daily_update": {"schedule": "16:16"}})
    assert got == ["ghost_leg"], f"孤儿键须显形: {got}"


# 钉②: 两集一致 → 空表
def test_no_orphan_when_aligned():
    got = _orphan({"daily_update": 7200}, {"daily_update": {"schedule": "16:16"}})
    assert got == []
    # 边界: 双 None / 双空
    assert _orphan(None, None) == []
    assert _orphan({}, {}) == []


# 钉③: check() 启动 warning 可见 (caplog)
def test_check_warns_on_orphan(caplog):
    from ops_layer.heartbeat_check import check
    hb_cfg = {"overrides": {"ghost_leg": 300}}
    pipelines = {"daily_update": {"schedule": "16:16", "days": "1-5"}}
    with caplog.at_level(logging.WARNING):
        check(pipelines, [], datetime(2026, 9, 28, 17, 0), hb_cfg)
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("ghost_leg" in m and "孤儿" in m for m in msgs), \
        f"孤儿键须 warning 显形: {msgs}"
