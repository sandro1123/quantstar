"""S4: 持有期复评状态机接 daily_update — 接线行为 + 原路径零回归"""
from datetime import date, timedelta

import pytest

from common.database import Database
from execution_layer import ExecutionLayer


def _weekdays_back(n: int) -> list[str]:
    """以今天为终点倒推 n 个工作日 (升序)。铺种交易日历用。"""
    days = []
    d = date.today()
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d.strftime("%Y-%m-%d"))
        d -= timedelta(days=1)
    return list(reversed(days))


@pytest.fixture()
def el():
    db = Database(":memory:")
    db.init_db()
    el = ExecutionLayer(db=db)
    el.start()
    yield el
    el.shutdown()
    db.close()


def _seed_calendar(el, days):
    conn = el.db.get_connection()
    # daily_klines 属数据层, init_db 不建 — 铺种前补最小表 (交易日历只读 date 列)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS daily_klines "
        "(code TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL)")
    for d in days:
        conn.execute(
            "INSERT INTO daily_klines (code, date, close) VALUES ('CAL', ?, 10.0)", (d,))
    conn.commit()


def test_daily_update_runs_due_review(el):
    """到期持仓 (持有>=5交易日) 经 daily_update 触发复评: 信号不足→中性续持+valid_until延长"""
    days = _weekdays_back(12)
    _seed_calendar(el, days)
    el.stock_pool.add("000001", "平安银行")
    el.stock_pool.update_status("000001", "active")
    el.stock_pool.update("000001", entry_date=days[-8])  # 持有 7 个交易日 >= 5

    el.daily_update()

    row = el.stock_pool.get("000001")
    assert row["valid_until"], "到期复评应延长 valid_until (中性续持)"
    conn = el.db.get_connection()
    holds = conn.execute(
        "SELECT COUNT(*) FROM action_records WHERE code='000001' AND action_type='hold'"
    ).fetchone()[0]
    assert holds >= 1, "复评续持应落 action_records hold 留痕"


def test_daily_update_skips_not_due(el):
    """未到期持仓 (持有<5交易日) 不触发复评: valid_until 保持空"""
    days = _weekdays_back(12)
    _seed_calendar(el, days)
    el.stock_pool.add("000002", "万科A")
    el.stock_pool.update_status("000002", "active")
    el.stock_pool.update("000002", entry_date=days[-2])  # 持有 1 个交易日 < 5

    el.daily_update()

    row = el.stock_pool.get("000002")
    assert not row["valid_until"], "未到期不应复评/延长 valid_until"


def test_daily_update_empty_pool_no_regression(el):
    """零回归守卫: 空池 daily_update 原路径完整跑完不抛错"""
    days = _weekdays_back(12)
    _seed_calendar(el, days)
    el.daily_update()  # 不抛异常即通过
