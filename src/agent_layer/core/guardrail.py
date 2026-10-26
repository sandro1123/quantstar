# -*- coding: utf-8 -*-
"""盘中防未来护栏 (AG-QUOTA-2 件4: quick 路径快速通过判定)。

护栏件 (防未来数据外泄面/防未来 LLM 调用膨胀):
- should_fast_pass: 现价与上次检查差 <0.3% 且无新信号 → True
  (走规则快速判定不进 LLM, 第一段实证盘中腿本就全规则 — 本件为
  升 LLM 版后的防线)
- is_near_line: 距触发线 <2% → True (临线才升 LLM 复核)
- record_last_check: last_check_price 落库 (供参数校准, 现价分布
  第一段判读定谳无落库)
"""
from __future__ import annotations

import time

from common.logging import get_logger

logger = get_logger("agent.guardrail")

FAST_PASS_PCT = 0.003   # 0.3% 价差阈值
NEAR_LINE_PCT = 0.02    # 2% 临线阈值

_LAST_CHECK_DDL = """
CREATE TABLE IF NOT EXISTS agent_last_check (
    code TEXT NOT NULL,
    task TEXT NOT NULL,
    last_price REAL,
    checked_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours')),
    PRIMARY KEY (code, task)
)
"""


def should_fast_pass(last_price: float | None, current_price: float,
                     new_signal: bool = False) -> bool:
    """现价与上次检查差 <0.3% 且无新信号 → 快速通过 (不进 LLM)。"""
    if new_signal:
        return False
    if not last_price or last_price <= 0 or current_price <= 0:
        return False
    return abs(current_price - last_price) / last_price < FAST_PASS_PCT


def is_near_line(current_price: float, trigger_price: float) -> bool:
    """距触发线 <2% → True (临线升 LLM 复核)。"""
    if not trigger_price or trigger_price <= 0 or current_price <= 0:
        return False
    return abs(current_price - trigger_price) / trigger_price < NEAR_LINE_PCT


def record_last_check(conn, code: str, task: str, last_price: float):
    """last_check_price 落库 (幂等 upsert)。"""
    conn.execute(_LAST_CHECK_DDL)
    conn.execute(
        "INSERT OR REPLACE INTO agent_last_check "
        "(code, task, last_price, checked_at) VALUES (?, ?, ?, ?)",
        (code, task, float(last_price),
         time.strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()


def get_last_check(conn, code: str, task: str) -> float | None:
    """读上次检查价 (无记录 → None, 调用方走真检查)。"""
    try:
        row = conn.execute(
            "SELECT last_price FROM agent_last_check "
            "WHERE code = ? AND task = ?", (code, task)).fetchone()
        return row[0] if row else None
    except Exception:  # noqa: BLE001
        return None
