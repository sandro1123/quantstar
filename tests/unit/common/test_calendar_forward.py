"""S5: 交易日历前瞻能力 — db 集合只有历史, 未来日期不得独断 False

生产事故 (2026-08-28): daily_klines 只到当日 (08-28), 22:57 挂入 pending_entry
时 next_trading_day 30 天循环穷尽 (未来日期全不在 db 集合 → is_trading_day 恒
False) → 返回原日期 → entry_valid_until 5 次原地踏步 → valid_until=挂入当日 =
挂入即零有效窗。002508 今晨 46 轮带内触价被拦。
"""
import pytest
from common.calendar import TradingCalendar
from common.database import Database


class _StubDb:
    """模拟生产 db: daily_klines 只有历史日期 (到 boundary 为止)。"""

    def __init__(self, dates):
        self._dates = list(dates)

    def get_connection(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE daily_klines (date TEXT)")
        conn.executemany("INSERT INTO daily_klines VALUES (?)",
                         [(d,) for d in self._dates])
        return conn


BOUNDARY = "2026-08-28"          # 生产实际 daily_klines MAX(date) (周五)
FUTURE1 = "2026-08-31"           # 周一 — 未来交易日
FUTURE2 = "2026-09-01"           # 周二


def _cal():
    return TradingCalendar(db=_StubDb(["2026-08-27", BOUNDARY]))


def test_future_date_not_absolutist_false():
    """db 集合边界之后的日期: 不得因"不在历史集合"独断 False (周一应推算为真)"""
    cal = _cal()
    assert cal.is_trading_day(FUTURE1) is True,         "边界后未来日期应回退静态推算 (2026-08-31 周一非节假日 = 交易日)"


def test_next_trading_day_crosses_db_boundary():
    """核心 RED: 边界日 next_trading_day 必须 > 当日 (不得原地踏步返回原值)"""
    cal = _cal()
    nxt = cal.next_trading_day(BOUNDARY)
    assert nxt > BOUNDARY, f"next_trading_day 穷尽回退返回原日期 {nxt}, valid_until=挂入日=零有效窗"


def test_next_trading_day_returns_next_weekday():
    """边界日 (周五) 的下一交易日 = 下周一 (2026-08-31)"""
    cal = _cal()
    assert cal.next_trading_day(BOUNDARY) == FUTURE1


def test_entry_valid_until_deep_night_mount():
    """深夜挂入场景 (生产 22:57 语义): db 日历只到当日, valid_until 必须在未来

    直接复现生产 writer 链: entry_valid_until (plan_manager) 在 db 日历边界日
    挂入 → 5 个交易日推进后 valid_until 必须 > 挂入日。
    """
    from execution_layer.core.plan_manager import entry_valid_until
    db = Database(":memory:")
    db.init_db()
    conn = db.get_connection()
    # db 日历只到 2026-08-28 (生产实况: daily_klines MAX(date)=08-28)
    conn.execute("CREATE TABLE IF NOT EXISTS daily_klines (date TEXT)")
    conn.executemany("INSERT INTO daily_klines VALUES (?)",
                     [("2026-08-27",), ("2026-08-28",)])
    conn.commit()
    got = entry_valid_until(db)
    assert got > "2026-08-28",         f"边界日挂入 valid_until={got} 不得等于挂入日 (挂入即零有效窗)"


def test_entry_valid_until_uses_config_days():
    """配置语义守卫: entry_valid_days=2 → 边界日(五)挂入 = 下周二

    intended validity = 配置默认 5 交易日 (execution.yaml/local.yaml 均无键,
    docstring 对齐策略A 5~10 交易日复评窗)。此测试钉住"天数语义不变, 只修算术"。
    """
    import pytest
    from execution_layer.core.plan_manager import entry_valid_until
    db = Database(":memory:")
    db.init_db()
    conn = db.get_connection()
    conn.execute("CREATE TABLE IF NOT EXISTS daily_klines (date TEXT)")
    conn.executemany("INSERT INTO daily_klines VALUES (?)",
                     [("2026-08-27",), ("2026-08-28",)])
    conn.commit()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("common.utils.bj_date", lambda: "2026-08-28")
        # monkeypatch 需钉被消费方 import 的引用 (plan_manager 已 from-import)
        import execution_layer.core.plan_manager as pm
        mp.setattr(pm, "bj_date", lambda: "2026-08-28")
        got = entry_valid_until(db, entry_valid_days=2)
    assert got == "2026-09-01",         f"08-28(五)+2 交易日应=09-01(二), got {got}"
