"""MemoryStore — memory_store 表读写 API (三铁律落点)

写入闸门: evidence 必指来源 + scope/kind 白名单 + 同 scope+kind 冲突 supersede 旧记忆。
读取预算: scope 过滤 (global 恒含) + recency×相关度 top-k + token 预算封顶。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from common.logging import get_logger

logger = get_logger("memory.store")

# scope 前缀白名单: global / agent:<name> / stock:<code> / factor:<name>
_SCOPE_PREFIXES = ("global", "agent:", "stock:", "factor:")
_KINDS = ("lesson", "feedback", "constraint")
_MAX_CONTENT = 500
_RECENCY_DECAY = 0.95  # 每日衰减系数


@dataclass
class Memory:
    id: int
    scope: str
    kind: str
    content: str
    evidence: str
    confidence: float
    created_at: str


def _est_tokens(text: str) -> int:
    """中文 token 估算: 1 字 ≈ 0.67 token → len/1.5 向上取整"""
    return int(len(text) / 1.5) + 1


class MemoryStore:
    def __init__(self, db):
        self.db = db
        self._ensure_mem_schema()
        self._ensure_ingested_at()

    def _ensure_ingested_at(self):
        """案C PIT 留口 (派单④): 旧库无 ingested_at 列时 ALTER 补列
        (守卫式扩展, 先例: 扩展表必配守卫 ALTER)。失败只 warning 不炸。"""
        try:
            conn = self.db.get_connection()
            cols = [r[1] for r in conn.execute(
                "PRAGMA table_info(memory_store)").fetchall()]
            if cols and "ingested_at" not in cols:
                conn.execute(
                    "ALTER TABLE memory_store ADD COLUMN ingested_at TEXT")
                conn.commit()
        except Exception as e:
            logger.warning(f"_ensure_ingested_at skip: {e}")

    def _ensure_mem_schema(self):
        """全新库 (memory_store 表不存在) 建全表 — 测试 :memory: 夹具同形。"""
        try:
            conn = self.db.get_connection()
            tabs = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            if "memory_store" not in tabs:
                # AUD-001: 复用 migrations v5 的唯一权威定义 (单一定义点) —
                # 旧实现另写一份, created_at 默认落 UTC (生产是 BJT)、丢 NOT NULL、
                # 丢两个索引 -> 测试库与生产库 schema 不同构 (假绿族)。
                from execution_layer.migrations import MEMORY_STORE_DDL

                for stmt in MEMORY_STORE_DDL:
                    conn.execute(stmt)
                # ingested_at 是本层的 PIT 留口 (案C), 不在中央清单里, 建表后补列
                conn.execute("ALTER TABLE memory_store ADD COLUMN ingested_at TEXT")
                conn.commit()
        except Exception as e:
            logger.warning(f"_ensure_mem_schema skip: {e}")

    def _conn(self):
        return self.db.get_connection()

    # ---- 写入 (铁律: 写入有闸门) ----
    def write(self, scope: str, kind: str, content: str, evidence: str,
              confidence: float = 0.5, expires_at: str | None = None,
              ingested_at: str | None = None) -> int | None:
        """ingested_at (案C PIT 留口): 该记忆所依据的数据入库时刻 — 与
        created_at (记忆本身写入时刻) 语义分离; 缺省 = 当前 BJT。"""
        if not evidence or not evidence.strip():
            logger.warning(f"MemoryStore.write 拒收: evidence 为空 (scope={scope})")
            return None
        if not scope or not any(scope == p.rstrip(":") or scope.startswith(p)
                                for p in _SCOPE_PREFIXES):
            logger.warning(f"MemoryStore.write 拒收: 非法 scope={scope!r}")
            return None
        if kind not in _KINDS:
            logger.warning(f"MemoryStore.write 拒收: 非法 kind={kind!r}")
            return None
        content = (content or "")[:_MAX_CONTENT]
        conn = self._conn()
        # 同 scope+kind 冲突: 旧 active 记忆 supersede (数据不是指令, 新证据覆盖旧结论)
        old_ids = [r[0] for r in conn.execute(
            "SELECT id FROM memory_store WHERE scope=? AND kind=? AND status='active'",
            (scope, kind))]
        _ing = ingested_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cur = conn.execute(
            "INSERT INTO memory_store (scope, kind, content, evidence, confidence, "
            "expires_at, ingested_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (scope, kind, content, evidence.strip(), confidence, expires_at, _ing))
        new_id = cur.lastrowid
        for oid in old_ids:
            conn.execute("UPDATE memory_store SET status='superseded', superseded_by=? WHERE id=?",
                         (new_id, oid))
        conn.commit()
        return new_id

    # ---- 读取 (铁律: 读取有预算) ----
    def read(self, scope: str | None = None, kind: str | None = None,
             query: str = "", top_k: int = 5, token_budget: int = 500) -> list[Memory]:
        sql = ("SELECT id, scope, kind, content, evidence, confidence, created_at "
               "FROM memory_store WHERE status='active' "
               "AND (expires_at IS NULL OR expires_at > datetime('now', '+8 hours'))")
        args: list = []
        if scope:
            sql += " AND (scope=? OR scope='global')"
            args.append(scope)
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        rows = self._conn().execute(sql, args).fetchall()
        now = datetime.now()
        scored = []
        q_chars = set(query or "")
        for r in rows:
            try:
                age_days = max(0.0, (now - datetime.strptime(r[6][:19], "%Y-%m-%d %H:%M:%S"))
                               .total_seconds() / 86400)
            except Exception:
                age_days = 0.0
            recency = _RECENCY_DECAY ** age_days
            overlap = len(q_chars & set(r[3])) if q_chars else 0
            relevance = 1.0 + overlap
            scored.append((recency * relevance, r))
        scored.sort(key=lambda x: -x[0])
        out: list[Memory] = []
        budget = token_budget
        for _, r in scored[:top_k * 3]:  # 多取再按预算截断
            cost = _est_tokens(r[3])
            if cost > budget:
                break
            budget -= cost
            out.append(Memory(id=r[0], scope=r[1], kind=r[2], content=r[3],
                              evidence=r[4], confidence=r[5], created_at=r[6]))
            if len(out) >= top_k:
                break
        return out
