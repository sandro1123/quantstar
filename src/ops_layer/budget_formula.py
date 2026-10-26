# -*- coding: utf-8 -*-
"""执行预算统一公式 — 单一定义点 (夜班 09-11 判例 22 收口)。

病根: 同一超时两个预算面不同源 — cron_watchdog/心跳 用 DL-65 ceiling
(timeout×(retries+1)+delay×retries, 依赖再取大等待窗), zombie reaper 用
裸 timeout+grace → reaper 先到点把活着的 ddp 误杀 (3000s < 5550s),
标记 failed 引发下游全链连锁 skip。

本模块 = ceiling 公式的唯一权威定义。watchdog 心跳面 (heartbeat_check.
budget_s) 与 zombie reaper (pipeline_logger.reap_expired_runs) 两消费
面都 import 此处 — 一处定义两处引用, 杜绝再漂移。

改公式必须 grep 全链 gate 面 (Mímir 判例): budget_formula.py /
heartbeat_check.py / cron_watchdog.sh 三处同步。
"""
from __future__ import annotations

# 依赖等待窗 (与 cron_watchdog.sh DL-65 同源)
DEP_WAIT_WINDOW_S = 900
DEP_MARGIN_S = 60


def compute_ceiling(pcfg: dict) -> int:
    """DL-65 统一公式: ceiling = t×(retries+1) + delay×retries。

    带依赖 (depends_on 非空) 再与等待窗 DEP_WAIT_WINDOW_S+DEP_MARGIN_S
    取大 — 依赖等待不算执行重试, 但预算须覆盖等待窗否则活行被误判。
    wait_timeout_s (锁等待窗, 09-04 锁饥饿族) 同理入预算面: 管道活着
    等锁的时长须计入预算, 否则等锁活行被 reaper 误杀 (本卡病历 ddp
    wait_timeout_s=2850 → 预算 = 2700+2850 = 5550s)。
    """
    t = int(pcfg.get("timeout_s", 300))
    retries = int(pcfg.get("max_retries", 0))
    delay = int(pcfg.get("retry_delay_s", 30))
    ceiling = t * (retries + 1) + delay * retries
    if pcfg.get("depends_on"):
        ceiling = max(ceiling, DEP_WAIT_WINDOW_S + DEP_MARGIN_S)
    wait = int(pcfg.get("wait_timeout_s", 0) or 0)
    if wait > 0:
        ceiling = max(ceiling, t + wait)
    return ceiling
