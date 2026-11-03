"""交易日历 — A 股交易日判断，支持从 DB 加载实际交易日

#14-b 静态表过期告警: 内置节假日表只覆盖到 _STATIC_COVERAGE_END。超过该日期
仍走静态 fallback 时, `is_trading_day` 会把未登记的节假日当交易日 — 静默失效。
Issue #14 原话「至少应在表过期时告警而非静默当作交易日」。故:
  - `static_table_stale_by(date)` 返回距表末的天数 (未过期 <=0);
  - `static_table_warning(date, lead_days)` 在临近/已过期时返回告警文案, 否则 None;
  - 调用方 (ops 健康检查) 拿它发 alert。本模块只出判据, 不发告警 — 保持纯函数。
"""
from datetime import datetime, timedelta

# 静态表覆盖的最后一个日期 (与本文件 _build_holidays 的最晚登记日同源,
# 改表必须同步改这里 — 测试 test_calendar_static_coverage_marker_matches_table 钉住)。
_STATIC_COVERAGE_END = "2027-12-31"


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
            "2026-06-19", "2026-06-20", "2026-06-21",   # 端午 (6-19 五放假, 周末本非交易日)
            "2026-09-25",                              # 中秋 (周五放假)
            "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05",
            "2026-10-06", "2026-10-07", "2026-10-08",   # 国庆长假
            # 2027 年常见节假日 (fallback 窗口覆盖)
            "2027-01-01",                              # 元旦 (周五放假)
            "2027-02-11", "2027-02-12", "2027-02-13", "2027-02-14", "2027-02-15",
            "2027-02-16", "2027-02-17",                # 春节长假
        }

    def _static_is_trading_day(self, date_str: str) -> bool:
        """静态推算: 周末 + 节假日表 (db 无数据时的既有 fallback 口径)"""
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        if dt.weekday() >= 5:
            return False
        return date_str not in self._holidays

    # -- #14-b 静态表过期判据 (纯函数, 不告警) ----------------------------

    @staticmethod
    def static_table_end() -> str:
        """静态表覆盖末日 (YYY-MM-DD)。"""
        return _STATIC_COVERAGE_END

    @staticmethod
    def static_table_stale_by(date_str: str) -> int:
        """距静态表末日的天数 — 已过期为正数, 未过期为 <=0。

        语义: 返回 >0 表示 date_str 已超出静态表覆盖, 此日之后的节假日
        不会被 _build_holidays 识别 (静默当作交易日)。
        """
        end = datetime.strptime(_STATIC_COVERAGE_END, "%Y-%m-%d")
        cur = datetime.strptime(date_str, "%Y-%m-%d")
        return (cur - end).days

    @staticmethod
    def static_table_warning(date_str: str, lead_days: int = 90) -> str | None:
        """临近/超过静态表末日时返回告警文案; 未临近返回 None。

        lead_days: 提前多少天开始提醒 (默认 90 天 ≈ 一个季度, 足够排期补表)。
        返回 None 是正常态 — 调用方据 None/非 None 决定是否发告警。
        """
        stale = TradingCalendar.static_table_stale_by(date_str)
        if stale > 0:
            return (f"交易日历静态表已过期 {stale} 天 (覆盖至 "
                    f"{_STATIC_COVERAGE_END}, 当前判断日 {date_str}) — "
                    f"此后日期按「周末+已知节假日」推算, 未登记的节假日会被"
                    f"误判为交易日, 交易日计数/毕业门禁/回测窗口全部受影响。"
                    f"请更新 src/common/calendar.py 的 _build_holidays 与 "
                    f"_STATIC_COVERAGE_END。")
        if stale >= -lead_days:
            return (f"交易日历静态表将于 {_STATIC_COVERAGE_END} 到期 "
                    f"(剩 {-stale} 天, 提醒阈值 {lead_days} 天) — "
                    f"请提前登记下一年的节假日表。")
        return None

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
