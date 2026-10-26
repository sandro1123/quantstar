"""ops_layer/lock_probe.py — Q4: BEGIN IMMEDIATE 锁健康探针

目的: 给「SQLite 写锁是否可获取」一个进程外的独立实证。08-21 ghost 持锁 6h47m
事故里, 锁被占这件事只能从「别的写入全部超时」间接推断; 本探针直接尝试
BEGIN IMMEDIATE, 成功=锁健康, 失败(locked)=锁被占, 把隐性问题变成显式信号。

铁律 #9 例外已获批 (统筹), 带三约束:
  1. 短 busy_timeout (默认 2s) — 探针绝不长时间阻塞/占锁
  2. try/finally rollback — 无论成败必释放, 探针自身永不成为新的持锁者
  3. 频率限制 (默认 ≥60s, 状态文件节流) — 不与正常写入抢锁
另保留 ro 降级路径: BEGIN IMMEDIATE 因非 locked 原因失败时, 退回只读
PRAGMA 健康检查, 不因探针自身故障误报锁问题。

三分支返回 (供 CLI/watchdog 接线):
  "healthy"  — BEGIN IMMEDIATE 成功, 锁可获取 (并 resolve 已结束的 ghost)
  "locked"   — BEGIN IMMEDIATE 撞 locked, 写锁被占 (critical 告警候选)
  "degraded" — 探针自身异常, 降级 ro 检查 (不误报锁问题)
"""

from __future__ import annotations

import os
import sqlite3
import time

from common.logging import get_logger

logger = get_logger("ops.lock_probe")

# 三约束默认值 (ops.yaml ops.lock_probe 可覆盖)
DEFAULT_BUSY_TIMEOUT_MS = 2000     # 约束1: 短 busy_timeout
DEFAULT_MIN_INTERVAL_S = 60        # 约束3: 频率限制
_STATE_ENV = "QS_LOCK_PROBE_STATE"  # 节流状态文件 (可覆盖, 供测试)


def _state_path() -> str:
    override = os.environ.get(_STATE_ENV)
    if override:
        return override
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(project_root, "logs", "lock_probe_state")


def _throttled(min_interval_s: float) -> bool:
    """约束3: 距上次探针不足 min_interval_s 则跳过 (返回 True=被节流)。"""
    path = _state_path()
    now = time.time()
    try:
        if os.path.exists(path):
            last = float(open(path, encoding="utf-8").read().strip() or 0)
            if now - last < min_interval_s:
                return True
    except Exception:
        pass  # 状态文件损坏不阻塞探针, 视为未节流
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(now))
    except Exception as e:
        logger.warning(f"lock_probe 状态文件写入失败 (不影响本次探测): {e}")
    return False


def probe(db_path: str,
          busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
          min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
          force: bool = False) -> dict:
    """尝试 BEGIN IMMEDIATE 实证写锁可获取性。

    返回 dict: {"status": "healthy"|"locked"|"degraded"|"throttled",
                "detail": str, "elapsed_ms": float}
    约束2: try/finally 保证 rollback + close, 探针绝不残留持锁。
    """
    if not force and _throttled(min_interval_s):
        return {"status": "throttled", "detail": f"距上次探针<{min_interval_s}s, 跳过", "elapsed_ms": 0.0}

    t0 = time.time()
    conn = None
    try:
        # 约束1: 短 busy_timeout — 撞锁最多等 busy_timeout_ms 即返回 locked
        conn = sqlite3.connect(db_path, timeout=busy_timeout_ms / 1000.0)
        conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        conn.execute("BEGIN IMMEDIATE")
        # 到这里 = 写锁已获取 = 锁健康。立即 rollback 释放 (约束2)。
        elapsed = (time.time() - t0) * 1000
        return {"status": "healthy",
                "detail": f"BEGIN IMMEDIATE 成功, 写锁可获取 ({elapsed:.0f}ms)",
                "elapsed_ms": elapsed}
    except sqlite3.OperationalError as e:
        if "locked" in str(e).lower() or "busy" in str(e).lower():
            # 分支B: 写锁被占 — 08-21 ghost 持锁的显式信号
            elapsed = (time.time() - t0) * 1000
            return {"status": "locked",
                    "detail": f"BEGIN IMMEDIATE 撞锁 ({elapsed:.0f}ms): {e}",
                    "elapsed_ms": elapsed}
        # 分支C: 非 locked 的 OperationalError — 探针自身/库问题, 降级 ro
        return _ro_degrade(db_path, f"非locked OperationalError: {e}", t0)
    except Exception as e:
        return _ro_degrade(db_path, f"探针异常: {e}", t0)
    finally:
        # 约束2: 无论何分支, rollback 释放 + close, 探针绝不残留持锁
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass


def _ro_degrade(db_path: str, reason: str, t0: float) -> dict:
    """ro 降级路径: BEGIN IMMEDIATE 因非 locked 原因失败时, 退回只读健康检查。

    只读打开 (mode=ro) 跑 PRAGMA quick_check, 确认库本身可读;
    不因探针写路径的自身故障误报「锁被占」。
    """
    try:
        ro = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
        try:
            row = ro.execute("PRAGMA quick_check").fetchone()
            ok = row and row[0] == "ok"
        finally:
            ro.close()
        return {"status": "degraded",
                "detail": f"{reason}; ro降级 quick_check={'ok' if ok else 'FAIL'}",
                "elapsed_ms": (time.time() - t0) * 1000}
    except Exception as e2:
        return {"status": "degraded",
                "detail": f"{reason}; ro降级也失败: {e2}",
                "elapsed_ms": (time.time() - t0) * 1000}
