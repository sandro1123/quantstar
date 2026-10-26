"""Agent Quality Repository — 所有 quality/ SQL 操作的统一入口"""

from common.database import Database


class AgentQualityRepository:
    """封装 agent_predictions 和 agent_memory 表的所有 SQL 操作"""

    def __init__(self, db: Database | None = None):
        self.db = db

    def ensure_tables(self):
        if not self.db:
            return
        conn = self.db.get_connection()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL,
                agent_name TEXT NOT NULL,
                code TEXT NOT NULL,
                date TEXT NOT NULL,
                verdict TEXT NOT NULL,
                confidence REAL NOT NULL,
                actual_return REAL,
                hit INTEGER,
                created_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_memory (
                code TEXT NOT NULL,
                date TEXT NOT NULL,
                verdict TEXT NOT NULL,
                confidence REAL NOT NULL,
                reasoning TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours')),
                PRIMARY KEY (code, date)
            )
        """)
        conn.commit()

    # === agent_predictions ===
    def write_prediction(self, request_id, agent_name, code, date, verdict, confidence):
        if not self.db:
            return
        conn = self.db.get_connection()
        conn.execute(
            "INSERT INTO agent_predictions (request_id, agent_name, code, date, verdict, confidence) VALUES (?, ?, ?, ?, ?, ?)",
            (request_id, agent_name, code, date, verdict, confidence),
        )
        conn.commit()

    def get_predictions_by_date(self, date):
        if not self.db:
            return []
        conn = self.db.get_connection()
        return [
            dict(r)
            for r in conn.execute(
                "SELECT id, code, verdict FROM agent_predictions WHERE date = ?",
                (date,),
            ).fetchall()
        ]

    def update_prediction_result(self, pred_id, actual_return, hit):
        if not self.db:
            return
        conn = self.db.get_connection()
        conn.execute(
            "UPDATE agent_predictions SET actual_return = ?, hit = ? WHERE id = ?",
            (actual_return, hit, pred_id),
        )
        conn.commit()

    def get_unresolved_predictions(self, date):
        if not self.db:
            return []
        conn = self.db.get_connection()
        return [
            dict(r)
            for r in conn.execute(
                "SELECT id, code, verdict, confidence FROM agent_predictions WHERE date = ? AND hit IS NULL",
                (date,),
            ).fetchall()
        ]

    def get_accuracy_stats(self, agent_name=None, days=30):
        if not self.db:
            return {"total": 0, "hits": 0, "accuracy": 0.0}
        conn = self.db.get_connection()
        cutoff = f"-{days} days"
        if agent_name:
            rows = conn.execute(
                "SELECT hit FROM agent_predictions WHERE agent_name = ? AND hit IS NOT NULL AND created_at >= date('now', ?, '+8 hours')",
                (agent_name, cutoff),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT hit FROM agent_predictions WHERE hit IS NOT NULL AND created_at >= date('now', ?, '+8 hours')",
                (cutoff,),
            ).fetchall()
        total = len(rows)
        hits = sum(1 for r in rows if r["hit"])
        return {"total": total, "hits": hits, "accuracy": hits / total if total else 0.0}

    def get_accuracy_sum(self, agent_name=None, days=30):
        if not self.db:
            return (0, 0)
        conn = self.db.get_connection()
        cutoff = f"-{days} days"
        if agent_name:
            row = conn.execute(
                "SELECT COUNT(*) as total, SUM(hit) as hits FROM agent_predictions WHERE agent_name = ? AND hit IS NOT NULL AND date >= date('now', ?, '+8 hours')",
                (agent_name, cutoff),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) as total, SUM(hit) as hits FROM agent_predictions WHERE hit IS NOT NULL AND date >= date('now', ?, '+8 hours')",
                (cutoff,),
            ).fetchone()
        return (row["total"] or 0, row["hits"] or 0)

    # === agent_memory ===
    def memory_remember(self, code, date, verdict, confidence, reasoning=""):
        if not self.db:
            return
        conn = self.db.get_connection()
        conn.execute(
            "INSERT OR REPLACE INTO agent_memory (code, date, verdict, confidence, reasoning) VALUES (?, ?, ?, ?, ?)",
            (code, date, verdict, confidence, reasoning),
        )
        conn.commit()

    def memory_recall(self, code):
        if not self.db:
            return None
        conn = self.db.get_connection()
        row = conn.execute(
            "SELECT * FROM agent_memory WHERE code = ? ORDER BY date DESC LIMIT 1",
            (code,),
        ).fetchone()
        return dict(row) if row else None

    def memory_recall_history(self, code, limit=5):
        if not self.db:
            return []
        conn = self.db.get_connection()
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM agent_memory WHERE code = ? ORDER BY date DESC LIMIT ?",
                (code, limit),
            ).fetchall()
        ]
