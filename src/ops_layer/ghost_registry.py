"""ops_layer/ghost_registry.py — P0-7 方向C: ghost future 登记 + 有界等待 + 结构化观测

根因 (2026-08-21 事故): `future.result(timeout)` 只放弃等待, `cancel()` 对运行中线程
是 no-op; ghost 线程在 orchestrator executor 上继续跑, 解释器退出时 concurrent.futures
全局 atexit join 全部非 daemon 线程 → 进程挂起并持有 DB 写锁 6h47m。

本模块不尝试"杀死" ghost (Python 线程杀不掉是语言限制), 而是让它可观测 (铁律 #12):
- register(): 超时后登记 ghost, 事件写 logs/ghost_registry.jsonl
- active()/snapshot(): 可查询当前存活/历史 ghost
- wait_active(timeout): shutdown 时有界等待 (而非依赖全局 atexit 无限 join)

红线: 不做 shutdown(wait=False) 掐断 ghost 的老路 (2026-08-17 静默丢数据事故);
登记与等待均为只读观测, 不干预 ghost 线程本身的执行。
"""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field

from common.logging import get_logger
from common.utils import bj_now

logger = get_logger("ops.ghost_registry")


def _ghost_jsonl_path() -> str:
    """ghost 事件 jsonl 路径 (QS_GHOST_REGISTRY 可覆盖, 供测试)。"""
    override = os.environ.get("QS_GHOST_REGISTRY")
    if override:
        return override
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(project_root, "logs", "ghost_registry.jsonl")


@dataclass
class GhostRecord:
    pipeline_name: str
    future: Future
    timeout_s: float
    registered_at: str = field(default_factory=bj_now)
    run_id: str | None = None
    resolved_at: str | None = None  # Q4: 锁探针确认锁释放/ghost 不再持锁时置位

    @property
    def alive(self) -> bool:
        return not self.future.done()

    @property
    def resolved(self) -> bool:
        return self.resolved_at is not None


class GhostRegistry:
    """超时 ghost 线程的登记表 — 只登记/查询/有界等待, 不杀线程。"""

    def __init__(self):
        self._records: list[GhostRecord] = []
        self._lock = threading.Lock()

    def register(self, pipeline_name: str, future: Future, timeout_s: float,
                 run_id: str | None = None) -> GhostRecord:
        """登记一个超时后仍在运行的 future, 并写 jsonl 事件。"""
        rec = GhostRecord(pipeline_name=pipeline_name, future=future,
                          timeout_s=timeout_s, run_id=run_id)
        with self._lock:
            self._records.append(rec)
        self._emit({
            "event": "ghost_registered",
            "ts": rec.registered_at,
            "pipeline": pipeline_name,
            "run_id": run_id,
            "timeout_s": timeout_s,
            "note": "timeout 后线程仍在运行; cancel() 对运行中线程无效, 进程退出时可能被全局 atexit join 拖住",
        })
        logger.warning(f"ghost registered: pipeline={pipeline_name} run_id={run_id} timeout={timeout_s}s")
        return rec

    def active(self) -> list[GhostRecord]:
        """当前仍存活的 ghost。"""
        with self._lock:
            records = list(self._records)
        return [r for r in records if r.alive]

    def snapshot(self) -> list[dict]:
        """全部登记记录 (含已结束的), 供查询/测试。"""
        with self._lock:
            records = list(self._records)
        return [{
            "pipeline": r.pipeline_name,
            "run_id": r.run_id,
            "timeout_s": r.timeout_s,
            "registered_at": r.registered_at,
            "alive": r.alive,
            "resolved": r.resolved,
        } for r in records]

    def wait_active(self, timeout: float) -> list[GhostRecord]:
        """有界等待全部存活 ghost 自然结束; 超时仍有存活者则返回它们。"""
        deadline = time.time() + timeout
        while True:
            survivors = self.active()
            if not survivors:
                return []
            if time.time() >= deadline:
                return survivors
            time.sleep(0.05)

    def resolve(self, pipeline_name: str | None = None) -> list[GhostRecord]:
        """Q4: 锁探针确认写锁可获取后调用 — 把已不存活(alive=False)的 ghost 标记 resolved。

        语义: ghost 线程结束(future.done)即不再持锁; 锁探针 BEGIN IMMEDIATE 成功
        是「锁已释放」的独立实证。两者一致时置 resolved_at 并写 jsonl 事件,
        给 08-21 类「ghost 持锁」事故一个可查询的闭环终态 (铁律 #12)。
        只标记已结束的 ghost; 仍存活的不动 (锁探针失败路径另行告警)。
        pipeline_name 给定时只解析该管道的 ghost。返回本次被解析的记录。
        """
        with self._lock:
            records = list(self._records)
        resolved = []
        for r in records:
            if r.resolved:
                continue
            if pipeline_name and r.pipeline_name != pipeline_name:
                continue
            if not r.alive:
                r.resolved_at = bj_now()
                resolved.append(r)
        for r in resolved:
            self._emit({
                "event": "ghost_resolved",
                "ts": r.resolved_at,
                "pipeline": r.pipeline_name,
                "run_id": r.run_id,
                "registered_at": r.registered_at,
                "note": "锁探针确认写锁可获取且 ghost 线程已结束, 闭环",
            })
        return resolved

    @staticmethod
    def _emit(event: dict) -> None:
        path = _ghost_jsonl_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        except Exception as e:  # 观测通道失败不阻塞主流程, 但必须出声 (铁律 #12)
            print(f"[ghost_registry][EVENT-FAILED] 无法写 jsonl: {e}; event={event}",
                  flush=True)


def emit_lock_released(detail: str = "") -> None:
    """Q4: 跨进程锁释放标记 — 锁探针确认写锁可获取后, 向 ghost jsonl 写
    ghost_lock_released 事件。与 ghost_registered 关联, 给「ghost 持锁」事故
    一个跨进程可查询的闭环终态 (铁律 #12)。

    GhostRegistry 的登记记录是进程内的 (orchestrator 被 watchdog 杀掉后即消失),
    但 jsonl 持久; 故跨进程闭环走 jsonl 标记而非内存 resolve()。
    resolve() 留给进程内调用方 (orchestrator 活着时确认锁后闭环)。
    """
    GhostRegistry._emit({
        "event": "ghost_lock_released",
        "ts": bj_now(),
        "note": detail or "lock_probe BEGIN IMMEDIATE 成功, 写锁已释放/可获取",
    })
