"""repos/adjust.py — 复权元数据 Repository (DL-35, D96 三表)

承载 adjust_factors / dividends / splits 三表的幂等建表 + 写入 + 读取。
表 DDL 复用 adjust_tables.py (DL-19 已建, 本 repo 只负责数据读写)。

口径 (D-40 改标记不改数据):
- adjust_factors.adjust_factor = **后复权因子** (baostock backAdjustFactor),
  随时间单调、向前稳定 (前复权会随未来除权变动, 存前复权是负担)。
  date = 除权除息日 (ex_date)。消费方跨除权日复权时, 取目标日期之前最近一个
  ex_date 的因子做比值即可, 无需存前复权。
- dividends.cash_dividend = 每股税前现金 (baostock dividCashPsBeforeTax, 元/股)。
- splits.split_factor = 送转后新股/旧股比 = 1 + 送股/股 + 转增/股。
"""

from __future__ import annotations

from common.database import Database
from common.logging import get_logger
from data_layer.adjust_tables import ADJUST_TABLES_DDL

logger = get_logger("data.adjust_repo")


class AdjustRepo:
    def __init__(self, db: Database):
        self.db = db

    def _ensure(self):
        conn = self.db.get_connection()
        conn.executescript(ADJUST_TABLES_DDL)
        conn.commit()

    # ---- 写 ----
    def upsert_adjust_factors(self, records: list[dict]) -> int:
        """records: [{code, ex_date, adjust_factor, source}]。返回写入行数。"""
        if not records:
            return 0
        self._ensure()
        conn = self.db.get_connection()
        conn.executemany(
            "INSERT OR REPLACE INTO adjust_factors (code, date, adjust_factor, source) "
            "VALUES (?, ?, ?, ?)",
            [(r["code"], r["ex_date"], r.get("adjust_factor", 1.0),
              r.get("source", "")) for r in records])
        conn.commit()
        return len(records)

    def upsert_dividends(self, records: list[dict]) -> int:
        """records: [{code, ex_date, announce_date, record_date, pay_date,
        cash_dividend, stock_dividend, stock_transfer, source}]。"""
        if not records:
            return 0
        self._ensure()
        conn = self.db.get_connection()
        conn.executemany(
            "INSERT OR REPLACE INTO dividends "
            "(code, ex_date, announce_date, record_date, pay_date, "
            " cash_dividend, stock_dividend, stock_transfer, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(r["code"], r["ex_date"], r.get("announce_date", ""),
              r.get("record_date", ""), r.get("pay_date", ""),
              r.get("cash_dividend", 0.0), r.get("stock_dividend", 0.0),
              r.get("stock_transfer", 0.0), r.get("source", "")) for r in records])
        conn.commit()
        return len(records)

    def upsert_splits(self, records: list[dict]) -> int:
        """records: [{code, ex_date, split_factor, source}]。"""
        if not records:
            return 0
        self._ensure()
        conn = self.db.get_connection()
        conn.executemany(
            "INSERT OR REPLACE INTO splits (code, ex_date, split_factor, source) "
            "VALUES (?, ?, ?, ?)",
            [(r["code"], r["ex_date"], r.get("split_factor", 1.0),
              r.get("source", "")) for r in records])
        conn.commit()
        return len(records)

    # ---- 读 ----
    def get_adjust_factor(self, code: str, date: str) -> float | None:
        """目标日期生效的后复权因子 = date 之前(含)最近一个 ex_date 的因子。

        无除权记录 → None (消费方按 1.0/不复权处理)。
        """
        self._ensure()
        conn = self.db.get_connection()
        row = conn.execute(
            "SELECT adjust_factor FROM adjust_factors "
            "WHERE code = ? AND date <= ? ORDER BY date DESC LIMIT 1",
            (code, date)).fetchone()
        return float(row["adjust_factor"]) if row and row["adjust_factor"] else None

    def get_factor_range(self, code: str, start_date: str, end_date: str) -> list[dict]:
        self._ensure()
        conn = self.db.get_connection()
        rows = conn.execute(
            "SELECT code, date AS ex_date, adjust_factor, source FROM adjust_factors "
            "WHERE code = ? AND date >= ? AND date <= ? ORDER BY date",
            (code, start_date, end_date)).fetchall()
        return [dict(r) for r in rows]

    def count(self, table: str) -> int:
        if table not in ("adjust_factors", "dividends", "splits"):
            return 0
        self._ensure()
        conn = self.db.get_connection()
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
