"""Agent 状态机 + Checkpoint — 持久化 + 类型安全 + 并行合并"""

import json
from dataclasses import dataclass, field
from enum import Enum

from common.database import Database
from common.utils import bj_now
from common.logging import get_logger

logger = get_logger("agent.state_machine")


class AgentState(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    DEGRADED = "degraded"
    TIMEOUT = "timeout"


@dataclass
class StateTransition:
    from_state: AgentState
    to_state: AgentState
    timestamp: str = ""
    reason: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class AgentCheckpoint:
    agent_name: str
    task: str
    state: AgentState = AgentState.PENDING
    transitions: list[StateTransition] = field(default_factory=list)
    result: dict = field(default_factory=dict)
    error: str = ""
    created_at: str = ""
    updated_at: str = ""
    trace_id: str = ""


class StateMachine:
    def __init__(self, db: Database | None = None):
        self.db = db
        self._checkpoints: dict[str, AgentCheckpoint] = {}
        self._valid_transitions = {
            AgentState.PENDING: {AgentState.RUNNING, AgentState.FAILED},
            AgentState.RUNNING: {AgentState.COMPLETED, AgentState.FAILED, AgentState.DEGRADED, AgentState.TIMEOUT},
            AgentState.DEGRADED: {AgentState.COMPLETED, AgentState.FAILED},
            AgentState.COMPLETED: set(),
            AgentState.FAILED: {AgentState.PENDING},
            AgentState.TIMEOUT: {AgentState.PENDING},
        }
        self._ensure_table()

    def create(self, agent_name: str, task: str, trace_id: str = "") -> str:
        key = f"{agent_name}:{task}:{trace_id or bj_now()}"
        now = bj_now()
        self._checkpoints[key] = AgentCheckpoint(
            agent_name=agent_name, task=task, state=AgentState.PENDING,
            created_at=now, updated_at=now, trace_id=trace_id or key,
        )
        self._persist(key)
        return key

    def transition(self, key: str, to_state: AgentState, reason: str = "", metadata: dict | None = None) -> bool:
        cp = self._checkpoints.get(key)
        if not cp:
            return False
        if to_state not in self._valid_transitions.get(cp.state, set()):
            return False
        now = bj_now()
        cp.transitions.append(StateTransition(
            from_state=cp.state, to_state=to_state,
            timestamp=now, reason=reason, metadata=metadata or {},
        ))
        cp.state = to_state
        cp.updated_at = now
        if to_state == AgentState.FAILED and reason:
            cp.error = reason
        self._persist(key)
        return True

    def set_result(self, key: str, result: dict):
        cp = self._checkpoints.get(key)
        if cp:
            cp.result = result
            self._persist(key)

    def get(self, key: str) -> AgentCheckpoint | None:
        return self._checkpoints.get(key)

    def _persist(self, key: str):
        if not self.db:
            return
        try:
            conn = self.db.get_connection()
            cp = self._checkpoints[key]
            conn.execute("""
                INSERT OR REPLACE INTO agent_checkpoints
                (checkpoint_key, agent_name, task, state, result_json, error, trace_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                key, cp.agent_name, cp.task, cp.state.value,
                json.dumps(cp.result, ensure_ascii=False),
                cp.error, cp.trace_id, cp.created_at, cp.updated_at,
            ))
            conn.commit()
        except Exception as e:
            # AUD-012: 旧实现裸 `except: pass`。检查点存在的意义是崩溃恢复 ——
            # 写失败静默 = 恢复时无可恢复且无人知 (撞写锁/连接被 close 时真实发生)。
            # 仍不上抛 (检查点是尽力而为, 不应拖垮 agent 主路径), 但必须留痕。
            logger.warning(f"agent checkpoint persist failed (key={key}): {e}")

    def recover(self, key: str) -> AgentCheckpoint | None:
        if not self.db:
            return None
        try:
            conn = self.db.get_connection()
            row = conn.execute(
                "SELECT * FROM agent_checkpoints WHERE checkpoint_key = ?", (key,)
            ).fetchone()
            if row:
                cp = AgentCheckpoint(
                    agent_name=row["agent_name"], task=row["task"],
                    state=AgentState(row["state"]),
                    result=json.loads(row["result_json"] or "{}"),
                    error=row["error"] or "", trace_id=row["trace_id"] or "",
                    created_at=row["created_at"], updated_at=row["updated_at"],
                )
                self._checkpoints[key] = cp
                return cp
        except Exception as e:
            # AUD-012: 同上 —— 读失败静默 = 从零重来且无信号。
            logger.warning(f"agent checkpoint recover failed (key={key}): {e}")
        return None

    def _ensure_table(self):
        if not self.db:
            return
        conn = self.db.get_connection()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_checkpoints (
                checkpoint_key TEXT PRIMARY KEY,
                agent_name TEXT NOT NULL,
                task TEXT NOT NULL,
                state TEXT NOT NULL,
                result_json TEXT,
                error TEXT,
                trace_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.commit()
