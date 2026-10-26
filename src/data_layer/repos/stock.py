"""repos/stock.py — 股票信息 Repository"""
from common.database import Database


def normalize_market(code: str, market: str = "") -> str:
    """规范化 market 字段: 按 code 前缀推导交易所。
    规则: 6*→SH, 0*/3*→SZ, 4*/8*/9*→BJ, 其余按传入值兜底。
    """
    if code.startswith("6"):
        return "SH"
    if code.startswith(("0", "3")):
        return "SZ"
    if code.startswith(("4", "8", "9")):
        return "BJ"
    return market or "SZ"


class StockRepo:
    def __init__(self, db: Database):
        self.db = db

    def _ensure_table(self):
        conn = self.db.get_connection()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stock_info (
                code TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                industry TEXT DEFAULT '',
                market TEXT DEFAULT 'SZ',
                list_date TEXT DEFAULT '',
                is_active INTEGER DEFAULT 1
            )
        """)
        conn.commit()

    def get_all_active(self) -> list[str]:
        self._ensure_table()
        conn = self.db.get_connection()
        rows = conn.execute(
            "SELECT code FROM stock_info WHERE is_active = 1"
        ).fetchall()
        return [r[0] for r in rows]

    def batch_upsert(self, records: list[dict]):
        self._ensure_table()
        conn = self.db.get_connection()
        for rec in records:
            market = normalize_market(
                rec["code"], rec.get("market", "")
            )
            # DL-21: ON CONFLICT 保留已有 list_date/industry — INSERT OR REPLACE
            # 会在每日同步 (akshare_sina 源仅 code+name) 时把这两字段重置为 '',
            # 抹掉回填值 (delist_date 不在 INSERT 列中, 同样得以保留)
            conn.execute("""
                INSERT INTO stock_info
                (code, name, industry, market, list_date, is_active)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET
                    name = excluded.name,
                    industry = CASE WHEN excluded.industry != ''
                                    THEN excluded.industry
                                    ELSE stock_info.industry END,
                    market = excluded.market,
                    list_date = CASE WHEN excluded.list_date != ''
                                     THEN excluded.list_date
                                     ELSE stock_info.list_date END,
                    is_active = excluded.is_active
            """, (
                rec["code"], rec["name"], rec.get("industry", ""),
                market, rec.get("list_date", ""),
                rec.get("is_active", 1),
            ))
        conn.commit()

    def get_by_code(self, code: str) -> dict | None:
        self._ensure_table()
        conn = self.db.get_connection()
        row = conn.execute(
            "SELECT * FROM stock_info WHERE code = ?", (code,)
        ).fetchone()
        return dict(row) if row else None
