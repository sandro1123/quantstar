"""数据质量门禁：覆盖率、日期格式、主键、价格与成交量合法性。"""

from dataclasses import dataclass, field
from datetime import datetime

from common.database import Database


@dataclass
class QualityReport:
    date: str
    flags: list[str] = field(default_factory=list)
    metrics: dict[str, int | float | str | None] = field(default_factory=dict)

    @property
    def is_passed(self) -> bool:
        return not self.flags


class DataQuality:
    """在数据写入和因子计算前执行的轻量质量门禁。"""

    DATE_TABLES = (
        "daily_klines",
        "factor_daily",
        "stock_financials",
        "stock_valuation",
        "stock_moneyflow",
    )
    DAILY_COVERAGE_RATIO = 0.85
    VALUATION_COVERAGE_RATIO = 0.80

    def __init__(self, db: Database):
        self.db = db

    def report(self, date: str) -> QualityReport:
        flags: list[str] = []
        metrics: dict[str, int | float | str | None] = {}
        if not self._is_canonical_date(date):
            return QualityReport(
                date=date,
                flags=[f"目标日期格式非法: {date}，必须为 YYYY-MM-DD"],
                metrics=metrics,
            )

        conn = self.db.get_connection()
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

        for table in self.DATE_TABLES:
            if table not in tables:
                flags.append(f"{table}: 表不存在")
                continue
            invalid_rows = self._invalid_date_rows(conn, table)
            metrics[f"{table}.invalid_date_rows"] = invalid_rows
            if invalid_rows:
                flags.append(f"{table}: 存在 {invalid_rows} 条非 YYYY-MM-DD 日期")

        if "daily_klines" in tables:
            kline_count = self._count_for_date(conn, "daily_klines", date)
            metrics["daily_klines.rows"] = kline_count
            active_count = self._active_stock_count(conn, tables)
            metrics["stock_info.active_rows"] = active_count
            coverage = kline_count / active_count if active_count else 0.0
            metrics["daily_klines.coverage_ratio"] = round(coverage, 6)
            if kline_count == 0:
                flags.append(f"daily_klines: {date} 无数据")
            elif active_count and coverage < self.DAILY_COVERAGE_RATIO:
                flags.append(
                    f"daily_klines: {date} 覆盖率 {coverage:.1%}，低于 "
                    f"{self.DAILY_COVERAGE_RATIO:.0%}"
                )

            duplicate_keys = self._duplicate_keys_for_date(conn, "daily_klines", date)
            metrics["daily_klines.duplicate_keys"] = duplicate_keys
            if duplicate_keys:
                flags.append(f"daily_klines: {date} 存在 {duplicate_keys} 个重复主键")

            invalid_ohlc = conn.execute(
                """
                SELECT COUNT(*) FROM daily_klines
                WHERE date = ? AND (
                    open IS NULL OR high IS NULL OR low IS NULL OR close IS NULL
                    OR open <= 0 OR high <= 0 OR low <= 0 OR close <= 0
                    OR high < MAX(open, close, low)
                    OR low > MIN(open, close, high)
                )
                """,
                (date,),
            ).fetchone()[0]
            metrics["daily_klines.invalid_ohlc_rows"] = invalid_ohlc
            if invalid_ohlc:
                flags.append(f"daily_klines: {date} 存在 {invalid_ohlc} 条非法 OHLC")

            invalid_trade = conn.execute(
                """
                SELECT COUNT(*) FROM daily_klines
                WHERE date = ? AND (
                    volume IS NULL OR amount IS NULL OR volume < 0 OR amount < 0
                )
                """,
                (date,),
            ).fetchone()[0]
            metrics["daily_klines.invalid_trade_rows"] = invalid_trade
            if invalid_trade:
                flags.append(
                    f"daily_klines: {date} 存在 {invalid_trade} 条非法成交量/成交额"
                )

        if "stock_valuation" in tables:
            valuation_count = self._count_for_date(conn, "stock_valuation", date)
            metrics["stock_valuation.rows"] = valuation_count
            kline_count = int(metrics.get("daily_klines.rows", 0) or 0)
            valuation_coverage = valuation_count / kline_count if kline_count else 0.0
            metrics["stock_valuation.coverage_ratio"] = round(valuation_coverage, 6)
            if kline_count and valuation_coverage < self.VALUATION_COVERAGE_RATIO:
                flags.append(
                    f"stock_valuation: {date} 相对行情覆盖率 {valuation_coverage:.1%}，"
                    f"低于 {self.VALUATION_COVERAGE_RATIO:.0%}"
                )

        return QualityReport(date=date, flags=flags, metrics=metrics)

    @staticmethod
    def _is_canonical_date(value: str) -> bool:
        try:
            return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d") == value
        except (TypeError, ValueError):
            return False

    def _invalid_date_rows(self, conn, table: str) -> int:
        invalid = 0
        for value, count in conn.execute(
            f'SELECT date, COUNT(*) FROM "{table}" GROUP BY date'
        ).fetchall():
            if not self._is_canonical_date(value):
                invalid += count
        return invalid

    @staticmethod
    def _count_for_date(conn, table: str, date: str) -> int:
        row = conn.execute(
            f'SELECT COUNT(*) FROM "{table}" WHERE date = ?', (date,)
        ).fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    def _active_stock_count(conn, tables: set[str]) -> int:
        if "stock_info" not in tables:
            return 0
        row = conn.execute(
            "SELECT COUNT(*) FROM stock_info WHERE COALESCE(is_active, 1) = 1"
        ).fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    def _duplicate_keys_for_date(conn, table: str, date: str) -> int:
        row = conn.execute(
            f'''
            SELECT COUNT(*) FROM (
                SELECT code, date FROM "{table}"
                WHERE date = ?
                GROUP BY code, date
                HAVING COUNT(*) > 1
            )
            ''',
            (date,),
        ).fetchone()
        return int(row[0]) if row else 0
