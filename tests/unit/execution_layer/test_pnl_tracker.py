"""PnLTracker 单元测试"""
import pytest
from common.database import Database
from execution_layer import ExecutionLayer


@pytest.fixture
def exec_layer():
    db = Database(":memory:")
    db.init_db()
    el = ExecutionLayer(db=db)
    el.start()
    yield el
    el.shutdown()
    db.close()


class MockKline:
    def __init__(self, code, close):
        self.code = code
        self.close = close


def test_get_pnl_trend_with_data(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "active")
    exec_layer.stock_pool.update("000001", entry_price=10.0, last_price=11.0,
                                  last_price_date="2026-07-22", unrealized_pnl_pct=10.0)
    trend = exec_layer.pnl_tracker.get_pnl_trend(days=30)
    assert len(trend) >= 1
    assert trend[0]["date"] == "2026-07-22"
    assert trend[0]["pnl_pct"] == 10.0


def test_get_pnl_trend_empty(exec_layer):
    trend = exec_layer.pnl_tracker.get_pnl_trend(days=30)
    assert trend == []


def test_daily_update_calculates_pnl(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "active")
    exec_layer.stock_pool.update("000001", entry_price=10.0)
    klines = [MockKline("000001", 11.0)]
    total_pnl = exec_layer.pnl_tracker.daily_update(klines)
    assert total_pnl == pytest.approx(10.0, abs=0.01)


def test_daily_update_multiple_stocks(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "active")
    exec_layer.stock_pool.update("000001", entry_price=10.0)
    exec_layer.stock_pool.add("000002", "万科A")
    exec_layer.stock_pool.update_status("000002", "active")
    exec_layer.stock_pool.update("000002", entry_price=20.0)
    klines = [MockKline("000001", 12.0), MockKline("000002", 18.0)]
    total_pnl = exec_layer.pnl_tracker.daily_update(klines)
    pnl_001 = (12.0 / 10.0 - 1) * 100
    pnl_002 = (18.0 / 20.0 - 1) * 100
    assert total_pnl == pytest.approx(pnl_001 + pnl_002, abs=0.01)


def test_daily_update_skips_missing_klines(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "active")
    exec_layer.stock_pool.update("000001", entry_price=10.0)
    total_pnl = exec_layer.pnl_tracker.daily_update([])
    assert total_pnl == 0.0


def test_daily_update_increments_holding_days(exec_layer, monkeypatch):
    monkeypatch.setattr(
        "execution_layer.tracking.pnl_tracker.bj_date",
        lambda: "2026-07-22",
    )
    monkeypatch.setattr("common.calendar.TradingCalendar.is_trading_day", lambda self, d: True)
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "active")
    exec_layer.stock_pool.update("000001", entry_price=10.0, holding_days=5)
    klines = [MockKline("000001", 11.0)]
    exec_layer.pnl_tracker.daily_update(klines)
    pool = exec_layer.stock_pool.get("000001")
    assert pool["holding_days"] == 6


def test_daily_update_handles_dict_klines(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "active")
    exec_layer.stock_pool.update("000001", entry_price=10.0)
    klines = [{"code": "000001", "close": 12.0}]
    total_pnl = exec_layer.pnl_tracker.daily_update(klines)
    assert total_pnl == pytest.approx(20.0, abs=0.01)


def test_daily_update_writes_action_on_large_change(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "active")
    exec_layer.stock_pool.update("000001", entry_price=10.0, unrealized_pnl_pct=0.0)
    klines = [MockKline("000001", 11.0)]
    exec_layer.pnl_tracker.daily_update(klines)
    actions = exec_layer.action_record.get_by_code("000001")
    pnl_actions = [a for a in actions if a["action_type"] == "pnl_change"]
    assert len(pnl_actions) == 1


def test_daily_update_no_action_on_small_change(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "active")
    exec_layer.stock_pool.update("000001", entry_price=10.0, unrealized_pnl_pct=9.0)
    klines = [MockKline("000001", 10.4)]
    exec_layer.pnl_tracker.daily_update(klines)
    actions = exec_layer.action_record.get_by_code("000001")
    pnl_actions = [a for a in actions if a["action_type"] == "pnl_change"]
    assert len(pnl_actions) == 0


def test_get_position_value_curve_returns_latest_state(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "active")
    exec_layer.stock_pool.update("000001", entry_price=10.0, last_price=13.0,
                                  last_price_date="2026-07-22", unrealized_pnl_pct=30.0)
    curve = exec_layer.pnl_tracker.get_position_value_curve("000001")
    assert len(curve) == 1
    assert curve[0]["date"] == "2026-07-22"
    assert curve[0]["price"] == 13.0
    assert curve[0]["pnl_pct"] == 30.0


def test_settle_pending_exits_settles_to_exited(exec_layer, monkeypatch):
    monkeypatch.setattr("execution_layer.bj_date", lambda: "2026-08-14")
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "exit_pending")
    exec_layer.stock_pool.update("000001", entry_price=10.0, position_qty=100, last_price=0)
    conn = exec_layer.db.get_connection()
    conn.execute("UPDATE stock_pool SET exit_reason='T+1测试' WHERE code='000001'")
    conn.execute("CREATE TABLE IF NOT EXISTS daily_klines (code TEXT, date TEXT, open REAL, "
                 "close REAL, high REAL, low REAL, volume INTEGER, amount REAL)")
    conn.execute("INSERT INTO daily_klines (code, date, open, close, high, low, volume, amount) "
                 "VALUES ('000001', '2026-08-14', 9.5, 10.5, 10.6, 9.4, 1000, 10000)")
    conn.commit()
    exec_layer._settle_pending_exits()
    pool = exec_layer.stock_pool.get("000001")
    assert pool["status"] == "exited"
    assert pool["exit_price"] == 10.5
    assert pool["exit_reason"] == "T+1测试"
    assert pool["total_return_pct"] == pytest.approx(5.0, abs=0.01)


def test_settle_pending_exits_skips_no_close(exec_layer, monkeypatch):
    monkeypatch.setattr("execution_layer.bj_date", lambda: "2026-08-14")
    exec_layer.stock_pool.add("000002", "万科A")
    exec_layer.stock_pool.update_status("000002", "exit_pending")
    exec_layer.stock_pool.update("000002", entry_price=10.0, position_qty=100, last_price=0)
    exec_layer._settle_pending_exits()
    pool = exec_layer.stock_pool.get("000002")
    assert pool["status"] == "exit_pending"


def test_daily_update_settles_pending_exits(exec_layer, monkeypatch):
    monkeypatch.setattr("execution_layer.bj_date", lambda: "2026-08-14")
    exec_layer.stock_pool.add("000003", "中兴通讯")
    exec_layer.stock_pool.update_status("000003", "exit_pending")
    exec_layer.stock_pool.update("000003", entry_price=10.0, position_qty=100,
                                 last_price=0, exit_reason="T+1结算")
    conn = exec_layer.db.get_connection()
    conn.execute("CREATE TABLE IF NOT EXISTS daily_klines (code TEXT, date TEXT, open REAL, "
                 "close REAL, high REAL, low REAL, volume INTEGER, amount REAL)")
    conn.execute("INSERT INTO daily_klines (code, date, open, close, high, low, volume, amount) "
                 "VALUES ('000003', '2026-08-14', 9.5, 10.5, 10.6, 9.4, 1000, 10000)")
    conn.commit()
    exec_layer.daily_update()
    pool = exec_layer.stock_pool.get("000003")
    assert pool["status"] == "exited"
