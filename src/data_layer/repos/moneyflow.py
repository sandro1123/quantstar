"""repos/moneyflow.py — 资金流 Repository"""
from common.database import Database


class MoneyflowRepo:
    def __init__(self, db: Database):
        self.db = db

    def _ensure_table(self):
        conn = self.db.get_connection()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stock_moneyflow (
                code TEXT NOT NULL,
                date TEXT NOT NULL,
                main_net_inflow REAL DEFAULT 0,
                retail_net_inflow REAL DEFAULT 0,
                main_inflow REAL DEFAULT 0,
                main_outflow REAL DEFAULT 0,
                PRIMARY KEY (code, date)
            )
        """)
        conn.commit()

    def batch_upsert(self, records: list[dict]):
        self._ensure_table()
        conn = self.db.get_connection()
        for rec in records:
            conn.execute("""
                INSERT OR REPLACE INTO stock_moneyflow
                (code, date, main_net_inflow, retail_net_inflow, main_inflow, main_outflow)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                rec["code"], rec["date"],
                rec.get("main_net_inflow", 0),
                rec.get("retail_net_inflow", 0),
                rec.get("main_inflow", 0),
                rec.get("main_outflow", 0),
            ))
        conn.commit()

    def get_range(self, code: str, start_date: str, end_date: str) -> list[dict]:
        conn = self.db.get_connection()
        rows = conn.execute(
            "SELECT * FROM stock_moneyflow WHERE code = ? AND date BETWEEN ? AND ? ORDER BY date",
            (code, start_date, end_date),
        ).fetchall()
        return [dict(r) for r in rows]
