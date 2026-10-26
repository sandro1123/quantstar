"""repos/valuation.py — 估值数据 Repository"""
from common.database import Database


class ValuationRepo:
    def __init__(self, db: Database):
        self.db = db

    def _ensure_table(self):
        conn = self.db.get_connection()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stock_valuation (
                code TEXT NOT NULL,
                date TEXT NOT NULL,
                pe_ttm REAL,
                pb_mrq REAL,
                ps_ttm REAL,
                market_cap REAL,
                total_shares REAL,
                dividend_yield REAL,
                PRIMARY KEY (code, date)
            )
        """)
        conn.commit()

    def batch_upsert(self, records: list[dict]):
        self._ensure_table()
        conn = self.db.get_connection()
        for rec in records:
            conn.execute("""
                INSERT OR REPLACE INTO stock_valuation
                (code, date, pe_ttm, pb_mrq, ps_ttm, market_cap, total_shares, dividend_yield)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                rec["code"], rec["date"], rec.get("pe_ttm"),
                rec.get("pb_mrq"), rec.get("ps_ttm"),
                rec.get("market_cap"), rec.get("total_shares"),
                rec.get("dividend_yield"),
            ))
        conn.commit()

    def get_range(self, code: str, start_date: str, end_date: str) -> list[dict]:
        self._ensure_table()
        conn = self.db.get_connection()
        rows = conn.execute("""
            SELECT * FROM stock_valuation
            WHERE code = ? AND date >= ? AND date <= ?
            ORDER BY date
        """, (code, start_date, end_date)).fetchall()
        return [dict(r) for r in rows]
