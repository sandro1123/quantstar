"""操作日志 — 统一记录所有操作"""

from common.database import Database


class ActionRecord:
    def __init__(self, db: Database):
        self.db = db

    def _ensure_table(self):
        from execution_layer.tables import ACTION_RECORDS_DDL
        conn = self.db.get_connection()
        conn.execute(ACTION_RECORDS_DDL)
        conn.commit()

    def write(self, code: str, action_type: str, reason: str, source: str = "execution",
              price: float | None = None, previous_value: float | None = None,
              new_value: float | None = None, action_detail: str = "", *, connection=None):
        if connection is None:
            self._ensure_table()
            conn = self.db.get_connection()
        else:
            # 调用方拥有事务；建表与 INSERT 均不得隐式提交其状态变更。
            from execution_layer.tables import ACTION_RECORDS_DDL
            conn = connection
            conn.execute(ACTION_RECORDS_DDL)
        conn.execute("""
            INSERT INTO action_records
            (code, action_type, action_detail, price, previous_value, new_value, reason, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (code, action_type, action_detail, price, previous_value, new_value, reason, source))
        if connection is None:
            conn.commit()

    def get_by_code(self, code: str, limit: int = 50) -> list[dict]:
        self._ensure_table()
        conn = self.db.get_connection()
        rows = conn.execute(
            "SELECT * FROM action_records WHERE code=? ORDER BY created_at DESC LIMIT ?",
            (code, limit)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_today(self) -> list[dict]:
        self._ensure_table()
        conn = self.db.get_connection()
        from common.utils import bj_date
        today = bj_date()
        rows = conn.execute(
            "SELECT * FROM action_records WHERE created_at LIKE ? ORDER BY created_at",
            (f"{today}%",)
        ).fetchall()
        return [dict(r) for r in rows]
