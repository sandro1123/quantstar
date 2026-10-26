# -*- coding: utf-8 -*-
"""配额管理 — 9router usageDaily 真源 (AG-QUOTA 第二段, 统筹 sm-908e7367 GO)

拆虚构账本: 原 register(total=1000) 进程内自记数=虚构 (第一段判读定谳)。
真源 = 9router db usageDaily 表 sqlite mode=ro 直读 (照 llm.yaml key_source
同款通道先例: type=sqlite path+query)。按 byApiKey[hint] 取当日真
requests/cost → remaining = 日预算 - 真消耗。

诚实降级 (禁静默宪法): 真源不可达 → self._degraded=True 显式状态位,
remaining_requests() 返回 None (绝不静默沿用虚构 1000)。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time

from common.logging import get_logger

logger = get_logger("agent.llm.quota")

# 10router 接入: key 与 9router 共享同库 (config/defaults/llm.yaml key_source 先例)。
# ⚠ 权威 = ops.yaml ops.agent.quota.source_path (ProviderManager 构造点直灌
# configure); 本常量仅 config 缺省时的降级兜底, 改路径须改 ops.yaml, 此值
# 须与 defaults 保持同步 — 严禁把这里当真源改 (默合假象判例 09-12)。
_DEFAULT_SOURCE_PATH = os.path.expanduser("~/.10router/db/data.sqlite")


class QuotaManager:
    """真源配额管理 — 原虚构账本语义全保 (register/record_usage 供旧测试),
    真源接线后 remaining_*=真值语义。"""

    def __init__(self):
        self._providers: dict[str, dict] = {}
        self._reserved = {
            "morning_report": 30,
            "evening_report": 20,
            "urgent_analysis": 10,
        }
        # AG-QUOTA-2 真源状态
        self._source_path: str | None = None
        self._api_key_hint: str | None = None
        self._date_key: str | None = None
        self._daily_budget_requests: int | None = None
        self._daily_budget_cost: float | None = None
        self._low_remaining_pct: float | None = None
        self._degraded: bool = False
        self._degraded_reason: str | None = None
        self._cache: tuple[float, dict] | None = None  # (ts, usage)

    # ---- 配置 (ops.yaml agent.quota 段键 → configure) ----
    def configure(self, daily_budget_requests: int | None = None,
                  daily_budget_cost: float | None = None,
                  source_path: str | None = None,
                  api_key_hint: str | None = None,
                  date_key: str | None = None,
                  low_remaining_pct: float | None = None):
        if daily_budget_requests is not None:
            self._daily_budget_requests = int(daily_budget_requests)
        if daily_budget_cost is not None:
            self._daily_budget_cost = float(daily_budget_cost)
        if source_path is not None:
            self._source_path = source_path
        if api_key_hint is not None:
            self._api_key_hint = api_key_hint
        if date_key is not None:
            self._date_key = date_key
        if low_remaining_pct is not None:
            self._low_remaining_pct = float(low_remaining_pct)

    # ---- 真源: sqlite mode=ro 直读 usageDaily (key_source 同款通道) ----
    @classmethod
    def fetch_usage_daily(cls, source_path: str, date_key: str,
                          api_key_hint: str | None = None) -> dict:
        """直读 9router usageDaily 表 → 当日用量 dict。

        mode=ro URI 物理禁写; 表缺/文件缺/JSON 坏 → raise (调用方降级)。
        """
        uri = "file:%s?mode=ro" % source_path
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            row = conn.execute(
                "SELECT data FROM usageDaily WHERE dateKey = ?",
                (date_key,)).fetchone()
            if not row:
                raise LookupError("usageDaily 无 %s 行" % date_key)
            data = json.loads(row[0])
            # 按 api key 细分 (有 hint 且命中 → 用细分面; 否则用全局面)
            if api_key_hint:
                by_key = (data.get("byApiKey") or {})
                for k, v in by_key.items():
                    if k.startswith(api_key_hint[:12]):
                        return {"requests": v.get("requests", 0),
                                "cost": v.get("cost", 0.0),
                                "scope": "byApiKey"}
            return {"requests": data.get("requests", 0),
                    "cost": data.get("cost", 0.0),
                    "scope": "total"}
        finally:
            conn.close()

    def is_degraded(self) -> bool:
        return self._degraded

    @property
    def degraded_reason(self) -> str | None:
        return self._degraded_reason

    def _today_usage(self) -> dict:
        """真源当日用量 (60s 缓存防高频读)。不可达 → degraded 显式位。"""
        if self._cache and time.time() - self._cache[0] < 60:
            return self._cache[1]
        path = self._source_path or _DEFAULT_SOURCE_PATH
        if not os.path.exists(path):
            self._degraded = True
            self._degraded_reason = "9router db 不存在: %s" % path
            raise LookupError(self._degraded_reason)
        try:
            usage = self.fetch_usage_daily(
                path, self._date_key or time.strftime("%Y-%m-%d"),
                self._api_key_hint)
            self._degraded = False
            self._degraded_reason = None
            self._cache = (time.time(), usage)
            return usage
        except Exception as e:  # noqa: BLE001
            self._degraded = True
            self._degraded_reason = "真源不可达: %s" % e
            logger.warning("[quota] 真源不可达 (degraded): %s", e)
            raise

    # ---- 真值 remaining 语义 (degraded → None 禁虚构) ----
    def remaining_requests(self) -> int | None:
        """日预算 - 当日真消耗。degraded/未配置预算 → None (禁静默虚构)。"""
        if self._daily_budget_requests is None:
            return None
        try:
            usage = self._today_usage()
        except LookupError:
            return None
        return max(0, self._daily_budget_requests - int(usage["requests"]))

    def remaining_cost(self) -> float | None:
        if self._daily_budget_cost is None:
            return None
        try:
            usage = self._today_usage()
        except LookupError:
            return None
        return max(0.0, self._daily_budget_cost - float(usage["cost"]))

    # ---- 低水位判定 (供降级评估) ----
    def is_low(self, pct: float = 0.30) -> bool | None:
        """剩余 < 预算×pct → True。degraded → None (调用方按保守处理)。"""
        rem = self.remaining_requests()
        if rem is None or self._daily_budget_requests is None:
            return None
        return rem < self._daily_budget_requests * pct

    # ---- 旧虚构账本面 (供既有测试/调用方兼容, 语义不变) ----
    def register(self, name: str, total: int = 1000):
        self._providers[name] = {"total": total, "used": 0,
                                 "reset_at": time.time() + 86400}

    def record_usage(self, name: str, count: int = 1):
        if name in self._providers:
            self._providers[name]["used"] += count

    def remaining(self, name: str) -> int:
        p = self._providers.get(name)
        if not p:
            return 0
        reserved = sum(self._reserved.values())
        return max(0, p["total"] - p["used"] - reserved)

    def select(self, task_type: str) -> str | None:
        available = []
        for name, p in self._providers.items():
            usable = self.remaining(name)
            if usable > 0:
                available.append((name, usable))
        if not available:
            return None
        available.sort(key=lambda x: -x[1])
        return available[0][0]

    def reserve_for_task(self, task: str, quota: int):
        self._reserved[task] = quota

    def health(self) -> dict:
        out = {n: {"total": p["total"], "used": p["used"],
                   "remaining": self.remaining(n)}
               for n, p in self._providers.items()}
        out["_source"] = {
            "path": self._source_path or _DEFAULT_SOURCE_PATH,
            "degraded": self._degraded,
            "reason": self._degraded_reason,
            "daily_budget_requests": self._daily_budget_requests,
            "remaining_requests": self.remaining_requests(),
        }
        return out
