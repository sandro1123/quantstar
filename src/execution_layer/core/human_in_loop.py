"""Human-in-loop — 关键决策的人工审核确认机制"""

from dataclasses import dataclass

from common.database import Database
from common.logging import get_logger
from common.utils import bj_now

logger = get_logger("execution.human_in_loop")


@dataclass
class ReviewDecision:
    action: str = ""
    reason: str = ""
    review_id: int = 0  # HITL: require_review 时为入队行 id, 供发审批卡与回填 message_id


_DECISION_TYPES = {
    "buy": "买入决策",
    "sell": "卖出决策",
    "add_position": "加仓决策",
    "reduce_position": "减仓决策",
    "swap": "调仓换股",
}

CONFIDENCE_THRESHOLD_AUTO = 0.7  # 高于此阈值自动执行
CONFIDENCE_THRESHOLD_REVIEW = 0.5  # 介于两者之间需要人工审核
# 低于 0.5 自动拒绝


class HumanInLoop:
    def __init__(self, db: Database):
        self.db = db
        self._ensure_table()

    # F4.6: 队列入库入场参数, approve 后消费转 pending_entry 需要
    _EXTRA_COLUMNS = {
        "entry_low": "REAL",
        "entry_high": "REAL",
        "stop_loss": "REAL",
        "take_profit": "REAL",
        "consumed": "INTEGER DEFAULT 0",
        "message_id": "TEXT",
    }

    def _ensure_table(self):
        conn = self.db.get_connection()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS human_review_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL,
                decision_type TEXT NOT NULL,
                reason TEXT,
                score REAL,
                confidence REAL,
                entry_low REAL,
                entry_high REAL,
                stop_loss REAL,
                take_profit REAL,
                consumed INTEGER DEFAULT 0,
                status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT (datetime('now', 'localtime')),
                reviewed_at TEXT,
                reviewer TEXT,
                review_note TEXT
            )
        """)
        # 旧表补列 (CREATE IF NOT EXISTS 不升级已存在表)
        existing = {r[1] for r in conn.execute("PRAGMA table_info(human_review_queue)")}
        for col, ctype in self._EXTRA_COLUMNS.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE human_review_queue ADD COLUMN {col} {ctype}")
        conn.commit()

    def evaluate(self, code: str, decision_type: str, reason: str,
                 score: float, confidence: float,
                 entry_low: float = 0, entry_high: float = 0,
                 stop_loss: float = 0, take_profit: float = 0) -> ReviewDecision:
        """评估决策是否需要人工审核"""
        if decision_type not in _DECISION_TYPES:
            decision_type = "buy"

        if confidence >= CONFIDENCE_THRESHOLD_AUTO:
            logger.info(f"Auto-approve {decision_type} for {code}: confidence={confidence:.2f}")
            return ReviewDecision(action="auto_approve", reason=f"置信度 {confidence:.2f} ≥ 自动执行阈值")

        if confidence >= CONFIDENCE_THRESHOLD_REVIEW:
            conn = self.db.get_connection()
            cur = conn.execute(
                "INSERT INTO human_review_queue (code, decision_type, reason, score, confidence, "
                "entry_low, entry_high, stop_loss, take_profit, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
                (code, decision_type, reason, score, confidence,
                 entry_low, entry_high, stop_loss, take_profit)
            )
            review_id = int(cur.lastrowid or 0)
            conn.commit()
            logger.info(f"Human review required for {code}: {decision_type} (confidence={confidence:.2f})")
            return ReviewDecision(action="require_review",
                                  reason=f"置信度 {confidence:.2f} 需人工审核",
                                  review_id=review_id)

        logger.info(f"Auto-reject {decision_type} for {code}: confidence={confidence:.2f}")
        return ReviewDecision(action="auto_reject", reason=f"置信度 {confidence:.2f} 低于执行阈值")

    def get(self, review_id: int) -> dict | None:
        conn = self.db.get_connection()
        row = conn.execute(
            "SELECT * FROM human_review_queue WHERE id = ?", (review_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_pending(self) -> list[dict]:
        conn = self.db.get_connection()
        rows = conn.execute(
            "SELECT * FROM human_review_queue WHERE status = 'pending' ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_approved_unconsumed(self) -> list[dict]:
        """F4.6: 已批准但尚未消费到 stock_pool 的审核记录"""
        conn = self.db.get_connection()
        rows = conn.execute(
            "SELECT * FROM human_review_queue WHERE status = 'approved' "
            "AND COALESCE(consumed, 0) = 0 ORDER BY reviewed_at"
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_consumed(self, review_id: int):
        conn = self.db.get_connection()
        conn.execute(
            "UPDATE human_review_queue SET consumed = 1 WHERE id = ?", (review_id,)
        )
        conn.commit()

    def set_message_id(self, review_id: int, message_id: str) -> bool:
        """HITL: 回填审批卡 message_id, 回调/兜底轮询靠它 PATCH 原卡片"""
        if not message_id:
            return False
        conn = self.db.get_connection()
        cur = conn.execute(
            "UPDATE human_review_queue SET message_id = ? WHERE id = ?",
            (message_id, review_id)
        )
        conn.commit()
        return cur.rowcount > 0

    def approve(self, review_id: int, reviewer: str = "human", note: str = "") -> bool:
        conn = self.db.get_connection()
        now = bj_now()
        cur = conn.execute(
            "UPDATE human_review_queue SET status = 'approved', reviewed_at = ?, reviewer = ?, review_note = ? WHERE id = ? AND status = 'pending'",
            (now, reviewer, note, review_id)
        )
        conn.commit()
        # rowcount 才是本语句影响行数 (total_changes 是连接累计, 恒 >0 的 bug)
        if cur.rowcount > 0:
            row = conn.execute("SELECT code, decision_type FROM human_review_queue WHERE id = ?", (review_id,)).fetchone()
            logger.info(f"Human approved {row['decision_type']} for {row['code']}")
            return True
        return False

    def reject(self, review_id: int, reviewer: str = "human", note: str = "") -> bool:
        conn = self.db.get_connection()
        now = bj_now()
        cur = conn.execute(
            "UPDATE human_review_queue SET status = 'rejected', reviewed_at = ?, reviewer = ?, review_note = ? WHERE id = ? AND status = 'pending'",
            (now, reviewer, note, review_id)
        )
        conn.commit()
        return cur.rowcount > 0

    def mark_invalid(self, review_id: int, reason: str = "") -> bool:
        """EX-01: 标记审核记录无效 (如无参数批准单) — 不消费不删除, 仅改状态留痕"""
        conn = self.db.get_connection()
        now = bj_now()
        cur = conn.execute(
            "UPDATE human_review_queue SET status = 'invalid', reviewed_at = ?, "
            "review_note = ? WHERE id = ? AND status IN ('pending', 'approved')",
            (now, reason, review_id)
        )
        conn.commit()
        if cur.rowcount > 0:
            logger.info(f"Review {review_id} marked invalid: {reason}")
            return True
        return False

    def veto(self, review_id: int, reviewer: str = "human", note: str = "") -> bool:
        """Phase 1.6: 否决一条已自动批准的记录 (事后人工干预)。

        与 reject 的区别: reject 只命中 pending; veto 命中 approved
        (consumed 与否都命中 — 消费标志在另一列)。已 rejected/invalid/
        已否决的行 rowcount=0 返回 False, 幂等。
        状态机外的联动 (撤 pending_entry / 退模拟仓) 由调用方按 consumed
        标志处置, 见 hitl_ws_daemon._apply_veto_side_effects。
        """
        conn = self.db.get_connection()
        now = bj_now()
        cur = conn.execute(
            "UPDATE human_review_queue SET status = 'rejected', reviewed_at = ?, "
            "reviewer = ?, review_note = ? WHERE id = ? AND status = 'approved'",
            (now, reviewer, note or "人工否决", review_id)
        )
        conn.commit()
        if cur.rowcount > 0:
            logger.info(f"Review {review_id} vetoed by {reviewer}")
            return True
        return False

    def get_stats(self) -> dict:
        conn = self.db.get_connection()
        total = conn.execute("SELECT COUNT(*) FROM human_review_queue").fetchone()[0]
        pending = conn.execute("SELECT COUNT(*) FROM human_review_queue WHERE status = 'pending'").fetchone()[0]
        approved = conn.execute("SELECT COUNT(*) FROM human_review_queue WHERE status = 'approved'").fetchone()[0]
        rejected = conn.execute("SELECT COUNT(*) FROM human_review_queue WHERE status = 'rejected'").fetchone()[0]
        return {
            "total": total,
            "pending": pending,
            "approved": approved,
            "rejected": rejected,
            "auto_approve_rate": round(approved / max(1, total), 2),
        }
