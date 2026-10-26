"""数据完整性校验 — 检查关键表行数 + 日期范围 + 异常"""

from common.database import Database
from common.logging import get_logger

logger = get_logger("data.integrity")


TABLE_EXPECTATIONS = {
    "daily_klines": {"min_rows": 1000000, "description": "日K线数据"},
    "stock_info": {"min_rows": 4000, "description": "股票基本信息"},
    "stock_valuation": {"min_rows": 4000, "description": "估值数据"},
    "stock_financials": {"min_rows": 10000, "description": "财务数据"},
    "factor_daily": {"min_rows": 100000, "description": "因子数据"},
    "stock_moneyflow": {"min_rows": 100000, "description": "资金流向"},
    "stock_pool": {"min_rows": 1, "description": "股票池"},
    "action_records": {"min_rows": 1, "description": "操作记录"},
}


class DataIntegrityChecker:
    def __init__(self, db: Database):
        self.db = db

    def check_all(self) -> dict:
        conn = self.db.get_connection()
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        results = {}
        for (table_name,) in tables:
            if table_name.startswith("sqlite_"):
                continue
            try:
                row = conn.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()
                count = row[0] if row else 0
                max_date = None
                date_col = self._guess_date_column(conn, table_name)
                if date_col:
                    row = conn.execute(f'SELECT MAX("{date_col}") FROM "{table_name}"').fetchone()
                    max_date = row[0] if row else None

                expectation = TABLE_EXPECTATIONS.get(table_name, {})
                min_rows = expectation.get("min_rows", 0)
                status = "ok" if count >= min_rows else "warning" if count > 0 else "error"

                results[table_name] = {
                    "rows": count,
                    "max_date": max_date,
                    "min_expected": min_rows,
                    "status": status,
                    "description": expectation.get("description", ""),
                }
            except Exception as e:
                results[table_name] = {"rows": -1, "status": "error", "error": str(e)}
        return results

    def _guess_date_column(self, conn, table_name: str) -> str | None:
        cols = conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()
        for c in cols:
            name = c[1].lower()
            if name in ("date", "trade_date", "created_at", "start_time", "calc_date", "deployed_at"):
                return c[1]
        return None

    def summary(self, results: dict | None = None) -> dict:
        if results is None:
            results = self.check_all()
        ok = sum(1 for v in results.values() if v.get("status") == "ok")
        warn = sum(1 for v in results.values() if v.get("status") == "warning")
        err = sum(1 for v in results.values() if v.get("status") == "error")
        errors = [k for k, v in results.items() if v.get("status") == "error"]
        warnings = [k for k, v in results.items() if v.get("status") == "warning"]
        return {
            "total_tables": len(results),
            "ok": ok,
            "warning": warn,
            "error": err,
            "error_tables": errors,
            "warning_tables": warnings,
        }
