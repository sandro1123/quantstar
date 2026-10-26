"""repos/kline.py — K 线 Repository"""
from common.database import Database


class KlineRepo:
    def __init__(self, db: Database):
        self.db = db
        self._quality_gate = None  # DL-29: 惰性质量闸

    def _gate(self):
        """DL-29: 写入路径质量闸 (只标记不改原值; fail-open 不阻塞写入)。"""
        if self._quality_gate is None:
            from data_layer.quality import KlineQualityGate
            self._quality_gate = KlineQualityGate(self.db)
        return self._quality_gate

    def _ensure_table(self):
        conn = self.db.get_connection()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_klines (
                code TEXT NOT NULL,
                date TEXT NOT NULL,
                open REAL DEFAULT 0,
                high REAL DEFAULT 0,
                low REAL DEFAULT 0,
                close REAL DEFAULT 0,
                volume REAL DEFAULT 0,
                amount REAL DEFAULT 0,
                pre_close REAL DEFAULT 0,
                change_pct REAL DEFAULT 0,
                turnover_rate REAL DEFAULT 0,
                adjust_type TEXT DEFAULT 'raw',
                PRIMARY KEY (code, date)
            )
        """)
        conn.commit()

    def batch_upsert(self, records: list[dict]):
        if not records:
            return
        records = self._gate().process(records, source="kline.batch_upsert")
        self._ensure_table()
        conn = self.db.get_connection()
        conn.executemany("""
            INSERT OR REPLACE INTO daily_klines
            (code, date, open, high, low, close, volume, amount,
             pre_close, change_pct, turnover_rate, adjust_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'raw')
        """, [(
            r["code"], r["date"], r.get("open", 0),
            r.get("high", 0), r.get("low", 0), r.get("close", 0),
            r.get("volume", 0), r.get("amount", 0),
            r.get("pre_close", 0), r.get("change_pct", 0),
            r.get("turnover_rate", 0),
        ) for r in records])
        conn.commit()

    def atomic_upsert_date(self, date: str, records: list[dict], min_rows: int = 0) -> int:
        """DL-03 (D91): 单日全量原子写入 — 单事务内写入+行数校验, 不达标整体回滚。

        返回写入行数; 校验失败 (写入行数 < min_rows) 时回滚并返回 -1, 表内不留半批数据。
        """
        if not records:
            return -1 if min_rows > 0 else 0
        records = self._gate().process(records, source="kline.atomic_upsert_date")
        self._ensure_table()
        conn = self.db.get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.executemany("""
                INSERT OR REPLACE INTO daily_klines
                (code, date, open, high, low, close, volume, amount,
                 pre_close, change_pct, turnover_rate, adjust_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'raw')
            """, [(
                r["code"], r["date"], r.get("open", 0),
                r.get("high", 0), r.get("low", 0), r.get("close", 0),
                r.get("volume", 0), r.get("amount", 0),
                r.get("pre_close", 0), r.get("change_pct", 0),
                r.get("turnover_rate", 0),
            ) for r in records])
            n = conn.execute(
                "SELECT COUNT(*) FROM daily_klines WHERE date = ?", (date,)
            ).fetchone()[0]
            if min_rows and n < min_rows:
                conn.execute("ROLLBACK")
                return -1
            conn.execute("COMMIT")
            return n
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise

    def get_range(self, code: str, start_date: str, end_date: str) -> list[dict]:
        self._ensure_table()
        conn = self.db.get_connection()
        rows = conn.execute("""
            SELECT * FROM daily_klines
            WHERE code = ? AND date >= ? AND date <= ?
            ORDER BY date
        """, (code, start_date, end_date)).fetchall()
        return [dict(r) for r in rows]

    def get_latest_date(self, code: str) -> str | None:
        self._ensure_table()
        conn = self.db.get_connection()
        row = conn.execute("""
            SELECT MAX(date) FROM daily_klines WHERE code = ?
        """, (code,)).fetchone()
        return row[0] if row and row[0] else None

    def get_codes_by_date(self, date: str) -> list[str]:
        """返回指定交易日有 K 线的代码集合（当日实际交易/停牌前有行）。"""
        self._ensure_table()
        conn = self.db.get_connection()
        rows = conn.execute(
            "SELECT DISTINCT code FROM daily_klines WHERE date = ?",
            (date,),
        ).fetchall()
        return [r[0] for r in rows]

    def get_max_date(self) -> str | None:
        self._ensure_table()
        conn = self.db.get_connection()
        row = conn.execute("SELECT MAX(date) FROM daily_klines").fetchone()
        return row[0] if row and row[0] else None

    def has_valid_change_pct(self, start_date: str, end_date: str,
                              threshold: float = 0.8) -> bool:
        """检查日期范围内是否有足够比例的有效 change_pct"""
        try:
            conn = self.db.get_connection()
            valid = conn.execute(
                "SELECT COUNT(*) FROM daily_klines WHERE date BETWEEN ? AND ? AND change_pct != 0",
                (start_date, end_date),
            ).fetchone()[0]
            total = conn.execute(
                "SELECT COUNT(*) FROM daily_klines WHERE date BETWEEN ? AND ?",
                (start_date, end_date),
            ).fetchone()[0]
            return total > 0 and valid / total >= threshold
        except Exception:
            return False
