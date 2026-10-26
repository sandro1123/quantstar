"""3层因子缓存: LRU内存 → DB(factor_daily) → 回退到实时计算"""

import time
from collections import OrderedDict

from common.database import Database
from common.logging import get_logger
from common.utils import validate_identifier
from data_layer.deprecated_columns import DEPRECATED_FACTOR_COLUMNS
from strategy_layer.factors.base import FactorResult

logger = get_logger("strategy.cache")

_VALID_COLUMNS: set[str] | None = None


def _get_valid_columns(db: Database) -> set[str]:
    global _VALID_COLUMNS
    if _VALID_COLUMNS is None:
        try:
            conn = db.get_connection()
            cols = [r[1] for r in conn.execute("PRAGMA table_info(factor_daily)").fetchall()]
            _VALID_COLUMNS = set(cols)
        except Exception:
            _VALID_COLUMNS = set()
    return _VALID_COLUMNS


class FactorCache:
    def __init__(self, db: Database | None = None, max_memory: int = 50000, ttl_s: int = 3600):
        self._db = db
        self._max_memory = max_memory
        self._ttl_s = ttl_s
        self._memory: OrderedDict[str, tuple[float, FactorResult]] = OrderedDict()
        self._stats = {"memory_hit": 0, "db_hit": 0, "compute": 0, "miss": 0}

    def _key(self, code: str, date: str, factor_name: str) -> str:
        return f"{code}:{date}:{factor_name}"

    def _evict_if_needed(self):
        while len(self._memory) > self._max_memory:
            self._memory.popitem(last=False)

    def get(self, code: str, date: str, factor_name: str) -> FactorResult | None:
        k = self._key(code, date, factor_name)

        # Layer 1: LRU memory cache
        cached = self._memory.get(k)
        if cached is not None:
            ts, result = cached
            if time.time() - ts < self._ttl_s:
                self._memory.move_to_end(k)
                self._stats["memory_hit"] += 1
                return result
            else:
                del self._memory[k]

        # Layer 2: DB cache (factor_daily table) — only if column exists
        # DL-39-exec (D-66): 已弃用的 31 个物理列自 2026-07-17 停写, 查询必 miss,
        # 直接跳过 DB 层落 Layer-3 现算 (省每因子一次必 miss 的查询)。
        if self._db is not None and factor_name not in DEPRECATED_FACTOR_COLUMNS:
            try:
                valid_cols = _get_valid_columns(self._db)
                if factor_name in valid_cols and validate_identifier(factor_name, "column"):
                    conn = self._db.get_connection()
                    row = conn.execute(
                        f"SELECT \"{factor_name}\" FROM factor_daily WHERE code = ? AND date = ?",
                        (code, date),
                    ).fetchone()
                    if row is not None and row[0] is not None:
                        val = row[0]
                        if isinstance(val, (int, float)):
                            self._stats["db_hit"] += 1
                            result = FactorResult(code=code, date=date, factor_name=factor_name, score=val, raw_value=val)
                            self._put_memory(k, result)
                            return result
            except Exception as e:
                logger.debug(f"DB cache miss for {k}: {e}")

        self._stats["miss"] += 1
        return None

    def put(self, code: str, date: str, factor_name: str, result: FactorResult):
        if not isinstance(result.score, (int, float)):
            return
        k = self._key(code, date, factor_name)
        self._put_memory(k, result)

    def _put_memory(self, key: str, result: FactorResult):
        self._memory[key] = (time.time(), result)
        self._evict_if_needed()

    def stats(self) -> dict:
        total = sum(self._stats.values())
        return {
            **self._stats,
            "total": total,
            "hit_rate": round((self._stats["memory_hit"] + self._stats["db_hit"]) / total * 100, 1) if total > 0 else 0,
            "memory_size": len(self._memory),
        }

    def clear(self):
        self._memory.clear()
        self._stats = {"memory_hit": 0, "db_hit": 0, "compute": 0, "miss": 0}

    def set_db(self, db: Database):
        self._db = db
