"""交易日历 — A 股交易日判断，支持从 DB 加载实际交易日"""
from datetime import datetime, timedelta


class TradingCalendar:
    def __init__(self, db=None):
        self._holidays = self._build_holidays()
        self._trading_days = None
        self._db = db

    def _load_from_db(self):
        if self._db is None:
            return
        try:
            conn = self._db.get_connection()
            rows = conn.execute(
                "SELECT DISTINCT date FROM daily_klines ORDER BY date"
            ).fetchall()
            self._trading_days = {r[0] for r in rows}
        except Exception:
            pass

    def _build_holidays(self) -> set:
        return {
            "2026-01-01", "2026-01-02", "2026-01-03",
            "2026-01-26", "2026-01-27", "2026-01-28", "2026-01-29", "2026-01-30",
            "2026-02-02", "2026-02-03", "2026-02-04", "2026-02-05", "2026-02-06",
            "2026-04-04", "2026-04-05", "2026-04-06",
            "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",
            "2026-06-25", "2026-06-26", "2026-06-29",
            "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05",
            "2026-10-06", "2026-10-07", "2026-10-08",
        }

    def _static_is_trading_day(self, date_str: str) -> bool:
        """静态推算: 周末 + 节假日表 (db 无数据时的既有 fallback 口径)"""
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        if dt.weekday() >= 5:
            return False
        return date_str not in self._holidays

    def is_trading_day(self, date_str: str) -> bool:
        if self._trading_days is None:
            self._load_from_db()
        if not self._trading_days:
            return self._static_is_trading_day(date_str)
        # S5 修复: db 集合全部是历史 (daily_klines DISTINCT date), 不含未来交易日。
        # 边界日之后的日期若独断 False, next_trading_day 30 天循环穷尽后返回原值,
        # 生产实证 (08-28): 挂入 pending_entry 的 valid_until=挂入当日=零有效窗,
        # 002508 带内触价 46 轮被拦。边界后回退静态推算, 不静默坏算术。
        if date_str > max(self._trading_days):
            return self._static_is_trading_day(date_str)
        return date_str in self._trading_days

    def next_trading_day(self, date_str: str) -> str:
        # S5 修复: 穷尽回退返回原值 = 断言「下一个交易日是今天」, 语义为假。
        # 穷尽后显式前推第一个静态推算交易日 (防御性, 正常不可达 — is_trading_day
        # 修复后未来日期可判定, 30 天窗口必命中)。
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        for _ in range(30):
            dt += timedelta(days=1)
            s = dt.strftime("%Y-%m-%d")
            if self.is_trading_day(s):
                return s
        dt2 = datetime.strptime(date_str, "%Y-%m-%d")
        for _ in range(365):
            dt2 += timedelta(days=1)
            s2 = dt2.strftime("%Y-%m-%d")
            if self._static_is_trading_day(s2):
                return s2
        return date_str

    def prev_trading_day(self, date_str: str) -> str:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        for _ in range(30):
            dt -= timedelta(days=1)
            s = dt.strftime("%Y-%m-%d")
            if self.is_trading_day(s):
                return s
        return date_str

    def trading_days_in_range(self, start: str, end: str) -> list[str]:
        result = []
        if self._trading_days is None:
            self._load_from_db()
        if self._trading_days:
            return sorted([d for d in self._trading_days if start <= d <= end])
        dt = datetime.strptime(start, "%Y-%m-%d")
        end_dt = datetime.strptime(end, "%Y-%m-%d")
        while dt <= end_dt:
            s = dt.strftime("%Y-%m-%d")
            if self.is_trading_day(s):
                result.append(s)
            dt += timedelta(days=1)
        return result

    def trading_days_between(self, start: str, end: str) -> int:
        return len(self.trading_days_in_range(start, end))
