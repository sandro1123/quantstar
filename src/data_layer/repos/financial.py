"""repos/financial.py — 财务数据 Repository"""
from common.database import Database

class FinancialRepo:
    def __init__(self, db: Database):
        self.db = db

    def _ensure_table(self):
        conn = self.db.get_connection()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stock_financials (
                code TEXT NOT NULL,
                date TEXT NOT NULL,
                eps REAL DEFAULT 0,
                roe REAL DEFAULT 0,
                revenue REAL DEFAULT 0,
                profit REAL DEFAULT 0,
                total_assets REAL DEFAULT 0,
                total_liabilities REAL DEFAULT 0,
                net_assets REAL DEFAULT 0,
                operating_cash_flow REAL DEFAULT 0,
                gross_margin REAL DEFAULT 0,
                net_margin REAL DEFAULT 0,
                announce_date TEXT DEFAULT '',
                caliber TEXT DEFAULT 'cumulative',
                PRIMARY KEY (code, date)
            )
        """)
        conn.commit()

    def _ensure_columns(self):
        """确保 announce_date 和 caliber 列存在（兼容旧表 ALTER）。"""
        conn = self.db.get_connection()
        existing = {r[1] for r in conn.execute("PRAGMA table_info(stock_financials)").fetchall()}
        if "announce_date" not in existing:
            conn.execute("ALTER TABLE stock_financials ADD COLUMN announce_date TEXT DEFAULT ''")
        if "caliber" not in existing:
            conn.execute("ALTER TABLE stock_financials ADD COLUMN caliber TEXT DEFAULT 'cumulative'")
        conn.commit()

    def batch_upsert(self, records: list[dict]):
        self._ensure_table()
        self._ensure_columns()
        conn = self.db.get_connection()
        for rec in records:
            conn.execute("""
                INSERT OR REPLACE INTO stock_financials
                (code, date, eps, roe, revenue, profit, total_assets,
                 total_liabilities, net_assets, operating_cash_flow,
                 gross_margin, net_margin, announce_date, caliber)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                rec["code"], rec["date"], rec.get("eps", 0),
                rec.get("roe", 0), rec.get("revenue", 0),
                rec.get("profit", 0), rec.get("total_assets", 0),
                rec.get("total_liabilities", 0), rec.get("net_assets", 0),
                rec.get("operating_cash_flow", 0),
                rec.get("gross_margin", 0), rec.get("net_margin", 0),
                rec.get("announce_date", ""), rec.get("caliber", "cumulative"),
            ))
        conn.commit()

    def get_range(self, code: str, start_date: str, end_date: str) -> list[dict]:
        self._ensure_table()
        conn = self.db.get_connection()
        rows = conn.execute("""
            SELECT * FROM stock_financials
            WHERE code = ? AND date >= ? AND date <= ?
            ORDER BY date
        """, (code, start_date, end_date)).fetchall()
        return [dict(r) for r in rows]

    def codes_for_date(self, date: str) -> set[str]:
        """返回指定日期(date)已覆盖的股票 code 集合（去重）。"""
        self._ensure_table()
        conn = self.db.get_connection()
        rows = conn.execute(
            "SELECT DISTINCT code FROM stock_financials WHERE date = ?", (date,)
        ).fetchall()
        return {r["code"] for r in rows}
