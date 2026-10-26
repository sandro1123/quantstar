#!/usr/bin/env python3
"""G4 盲区补偿 (2026-08-24夜): 补扫 observability_fallback.jsonl 升格为 alerts 行。

背景: quality_check/freshness_audit 型管道「死在 start_run 前」(看门狗预算 240s <
依赖等待窗 900s), 走不到 orchestrator 的 G4 埋点, 只留下 cron_watchdog 的
observability_fallback.jsonl 条目。本脚本由 health_check 管道调用 (或手动跑),
把未升格的 fallback 条目转成正式 alerts 行 — 铁律 #12: 观测不可静默。

只写 alerts 表 (INSERT OR IGNORE 幂等); 不碰 pipeline_runs。
"""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)

_FALLBACK_PATH = os.path.join("logs", "observability_fallback.jsonl")
# 已处理条目记账 (防重复升格): 记 fallback_ts + name 指纹
_STATE_PATH = os.path.join("logs", "g4_blindspot_state.json")


def _load_state() -> set[str]:
    try:
        with open(_STATE_PATH, encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save_state(seen: set[str]) -> None:
    try:
        with open(_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(sorted(seen)[-500:], f)  # 有界, 防无限增长
    except Exception as e:
        logger.debug(f"g4 blindspot state save failed (non-blocking): {e}")


def promote_fallback_entries(db) -> int:
    """扫描 fallback 日志, 把 pipeline_run 失败条目升格为 alerts。

    返回新增 alert 数; 任何失败都不抛 (fail-safe, 不影响 health_check 主流程)。
    """
    promoted = 0
    if not os.path.exists(_FALLBACK_PATH):
        return 0
    seen = _load_state()
    try:
        with open(_FALLBACK_PATH, encoding="utf-8", errors="replace") as f:
            lines = [l.strip() for l in f if l.strip()]
    except Exception as e:
        logger.warning(f"g4 blindspot: cannot read fallback log: {e}")
        return 0

    entries: list[dict] = []
    for line in lines:
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("kind") != "pipeline_run" or obj.get("status") != "failed":
            continue
        fp = f"{obj.get('name')}@{obj.get('fallback_ts')}"
        entries.append(obj)
        # 即使已见过也保留在 seen 里 (有界裁剪)

    for obj in entries:
        fp = f"{obj.get('name')}@{obj.get('fallback_ts')}"
        if fp in seen:
            continue
        title = f"观测降级: {obj.get('name')}(pipeline_run 未落库)"
        detail = (
            f"OPS-04 第二通道捕获: {obj.get('name')} 运行记录落库失败 "
            f"({obj.get('reason')}); 该管道可能死于 start_run 之前 "
            f"(watchdog 预算 < 依赖等待窗的盲区形态)。"
            f"version={obj.get('version')} ts={obj.get('fallback_ts')}"
        )
        try:
            conn = db.get_connection()
            cur = conn.execute(
                "INSERT OR IGNORE INTO alerts (severity, layer, title, detail, action, status) "
                "VALUES ('warning', 'ops', ?, ?, ?, 'open')",
                (
                    title,
                    detail,
                    "查 cron_watchdog 预算 vs ops.yaml timeout_s+等待窗; "
                    "若为依赖饿死盲区按 G4 规格 §1.2 补偿条款处置",
                ),
            )
            conn.commit()
            if cur.rowcount > 0:
                promoted += 1
            seen.add(fp)
        except Exception as e:
            logger.warning(f"g4 blindspot: insert alert failed: {e}")
            break  # 库异常时停止本轮, 下次重试
    _save_state(seen)
    return promoted


if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from common.database import Database
    n = promote_fallback_entries(Database())
    print(f"g4 blindspot: promoted {n} fallback entry(ies)")
