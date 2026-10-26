"""PostExitTracker 单元测试"""
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
    def __init__(self, close):
        self.close = close


def test_create_tracking_inserts_three_records(exec_layer):
    exec_layer.post_exit_tracker.create_tracking(
        code="000001", exit_date="2026-07-15", exit_price=15.0,
        entry_price=10.0, total_return_pct=50.0, exit_reason="take_profit"
    )
    records = exec_layer.post_exit_tracker.get_tracking("000001")
    assert len(records) == 3
    checkpoints = {r["check_point"] for r in records}
    assert checkpoints == {"T+5", "T+10", "T+20"}


def test_create_tracking_stores_correct_fields(exec_layer):
    exec_layer.post_exit_tracker.create_tracking(
        code="000001", exit_date="2026-07-15", exit_price=15.0,
        entry_price=10.0, total_return_pct=50.0, exit_reason="take_profit"
    )
    records = exec_layer.post_exit_tracker.get_tracking("000001")
    r = records[0]
    assert r["code"] == "000001"
    assert r["exit_date"] == "2026-07-15"
    assert r["exit_price"] == 15.0
    assert r["entry_price"] == 10.0
    assert r["total_return_pct"] == 50.0
    assert r["exit_reason"] == "take_profit"
    assert r["quality"] is None


def test_create_tracking_allows_duplicate_checkpoints(exec_layer):
    """重复 create_tracking 被唯一约束去重 (判例: 断言病态行为的测试, 改它才是修完)。

    旧断言 len==6 把双份 T+5/T+10/T+20 锁成契约; W1 任务D 加 UNIQUE 后二次
    create 幂等 (INSERT OR IGNORE 吃掉重复), 3 行才是正确状态。
    """
    exec_layer.post_exit_tracker.create_tracking(
        code="000001", exit_date="2026-07-15", exit_price=15.0,
        entry_price=10.0, total_return_pct=50.0, exit_reason="take_profit"
    )
    exec_layer.post_exit_tracker.create_tracking(
        code="000001", exit_date="2026-07-15", exit_price=15.0,
        entry_price=10.0, total_return_pct=50.0, exit_reason="take_profit"
    )
    records = exec_layer.post_exit_tracker.get_tracking("000001")
    assert len(records) == 3
    assert {r["check_point"] for r in records} == {"T+5", "T+10", "T+20"}


def test_check_correct_quality(exec_layer):
    exec_layer.post_exit_tracker.create_tracking(
        code="000001", exit_date="2026-07-15", exit_price=15.0,
        entry_price=10.0, total_return_pct=50.0, exit_reason="take_profit"
    )
    klines_by_code = {"000001": [MockKline(13.0)]}
    checked = exec_layer.post_exit_tracker.check(klines_by_code)
    assert checked == 3
    records = exec_layer.post_exit_tracker.get_tracking("000001")
    for r in records:
        assert r["quality"] == "correct"
        assert r["current_price"] == 13.0


def test_check_wrong_quality(exec_layer):
    exec_layer.post_exit_tracker.create_tracking(
        code="000001", exit_date="2026-07-15", exit_price=15.0,
        entry_price=10.0, total_return_pct=50.0, exit_reason="take_profit"
    )
    klines_by_code = {"000001": [MockKline(17.0)]}
    checked = exec_layer.post_exit_tracker.check(klines_by_code)
    assert checked == 3
    records = exec_layer.post_exit_tracker.get_tracking("000001")
    for r in records:
        assert r["quality"] == "wrong"


def test_check_neutral_quality(exec_layer):
    exec_layer.post_exit_tracker.create_tracking(
        code="000001", exit_date="2026-07-15", exit_price=15.0,
        entry_price=10.0, total_return_pct=50.0, exit_reason="take_profit"
    )
    klines_by_code = {"000001": [MockKline(15.2)]}
    checked = exec_layer.post_exit_tracker.check(klines_by_code)
    assert checked == 3
    records = exec_layer.post_exit_tracker.get_tracking("000001")
    for r in records:
        assert r["quality"] == "neutral"


def test_check_skips_already_checked(exec_layer):
    exec_layer.post_exit_tracker.create_tracking(
        code="000001", exit_date="2026-07-15", exit_price=15.0,
        entry_price=10.0, total_return_pct=50.0, exit_reason="take_profit"
    )
    klines_by_code = {"000001": [MockKline(13.0)]}
    exec_layer.post_exit_tracker.check(klines_by_code)
    klines_by_code2 = {"000001": [MockKline(17.0)]}
    checked = exec_layer.post_exit_tracker.check(klines_by_code2)
    assert checked == 0


def test_check_no_klines_skips(exec_layer):
    exec_layer.post_exit_tracker.create_tracking(
        code="000001", exit_date="2026-07-15", exit_price=15.0,
        entry_price=10.0, total_return_pct=50.0, exit_reason="take_profit"
    )
    checked = exec_layer.post_exit_tracker.check(None)
    assert checked == 0
    records = exec_layer.post_exit_tracker.get_tracking("000001")
    for r in records:
        assert r["quality"] is None


def test_get_tracking_empty(exec_layer):
    records = exec_layer.post_exit_tracker.get_tracking("NONEXIST")
    assert records == []


def test_check_writes_action_record(exec_layer):
    exec_layer.post_exit_tracker.create_tracking(
        code="000001", exit_date="2026-07-15", exit_price=15.0,
        entry_price=10.0, total_return_pct=50.0, exit_reason="take_profit"
    )
    klines_by_code = {"000001": [MockKline(13.0)]}
    exec_layer.post_exit_tracker.check(klines_by_code)
    actions = exec_layer.action_record.get_by_code("000001")
    tracking_actions = [a for a in actions if a["action_type"] == "exit_tracking_check"]
    assert len(tracking_actions) == 3


# ── F4.5: 结算接线 + 到期自查 daily_klines ──────────────────────────

def _seed_klines(exec_layer, code, dates_closes):
    conn = exec_layer.db.get_connection()
    conn.execute("CREATE TABLE IF NOT EXISTS daily_klines (code TEXT, date TEXT, close REAL, PRIMARY KEY (code, date))")
    for d, c in dates_closes:
        conn.execute("INSERT OR REPLACE INTO daily_klines (code, date, close) VALUES (?, ?, ?)", (code, d, c))
    conn.commit()


def test_settle_creates_tracking(exec_layer, monkeypatch):
    """F4.5: _settle_pending_exits 结算后自动创建 T+5/T+10/T+20 跟踪记录"""
    monkeypatch.setattr("execution_layer.bj_date", lambda: "2026-08-14")
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "exit_pending")
    exec_layer.stock_pool.update("000001", entry_price=10.0, position_qty=100, last_price=10.5)
    _seed_klines(exec_layer, "000001", [("2026-08-14", 10.5)])
    exec_layer._settle_pending_exits()
    records = exec_layer.post_exit_tracker.get_tracking("000001")
    assert len(records) == 3
    assert {r["check_point"] for r in records} == {"T+5", "T+10", "T+20"}
    assert all(r["exit_date"] == "2026-08-14" and r["exit_price"] == 10.5 for r in records)


def test_check_due_point_from_daily_klines(exec_layer):
    """F4.5: 无 klines 入参时自查 daily_klines, 仅到期检查点被评估"""
    exec_layer.post_exit_tracker.create_tracking(
        code="000001", exit_date="2026-08-01", exit_price=15.0,
        entry_price=10.0, total_return_pct=50.0, exit_reason="止损"
    )
    # 退出后 6 个交易日 → T+5 到期 (第5日收盘 13.0, 跌 13.3% → correct), T+10/T+20 未到期
    _seed_klines(exec_layer, "000001", [
        ("2026-08-04", 14.5), ("2026-08-05", 14.0), ("2026-08-06", 13.8),
        ("2026-08-07", 13.5), ("2026-08-08", 13.0), ("2026-08-11", 12.8),
    ])
    checked = exec_layer.post_exit_tracker.check()
    assert checked == 1
    records = {r["check_point"]: r for r in exec_layer.post_exit_tracker.get_tracking("000001")}
    assert records["T+5"]["quality"] == "correct"
    assert records["T+5"]["current_price"] == 13.0
    assert records["T+5"]["price_change_since_exit"] == pytest.approx((13.0 - 15.0) / 15.0)
    assert records["T+10"]["quality"] is None
    assert records["T+20"]["quality"] is None


def test_check_not_due_skips(exec_layer):
    """退出后交易日不足 → 全部未到期, 不评估"""
    exec_layer.post_exit_tracker.create_tracking(
        code="000001", exit_date="2026-08-01", exit_price=15.0,
        entry_price=10.0, total_return_pct=50.0, exit_reason="止损"
    )
    _seed_klines(exec_layer, "000001", [("2026-08-04", 14.5), ("2026-08-05", 14.0)])
    assert exec_layer.post_exit_tracker.check() == 0


def test_check_wrong_quality_from_klines(exec_layer):
    """到期后大涨 → wrong (卖飞了)"""
    exec_layer.post_exit_tracker.create_tracking(
        code="000001", exit_date="2026-08-01", exit_price=15.0,
        entry_price=10.0, total_return_pct=50.0, exit_reason="止损"
    )
    _seed_klines(exec_layer, "000001", [
        ("2026-08-04", 15.5), ("2026-08-05", 16.0), ("2026-08-06", 16.5),
        ("2026-08-07", 17.0), ("2026-08-08", 17.5),
    ])
    assert exec_layer.post_exit_tracker.check() == 1
    records = {r["check_point"]: r for r in exec_layer.post_exit_tracker.get_tracking("000001")}
    assert records["T+5"]["quality"] == "wrong"


def test_e2e_settle_then_due_check(exec_layer, monkeypatch):
    """端到端: 结算 → create_tracking → 到期 check → quality 判定"""
    monkeypatch.setattr("execution_layer.bj_date", lambda: "2026-08-14")
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "exit_pending")
    exec_layer.stock_pool.update("000001", entry_price=10.0, position_qty=100, last_price=10.5)
    _seed_klines(exec_layer, "000001", [("2026-08-14", 10.5)])
    exec_layer._settle_pending_exits()
    assert len(exec_layer.post_exit_tracker.get_tracking("000001")) == 3
    # 退出日 2026-08-14 后 5 个交易日, 第 5 日收盘 9.0 (跌 14.3% → correct)
    _seed_klines(exec_layer, "000001", [
        ("2026-08-15", 10.2), ("2026-08-18", 10.0), ("2026-08-19", 9.8),
        ("2026-08-20", 9.5), ("2026-08-21", 9.0),
    ])
    checked = exec_layer.post_exit_tracker.check()
    assert checked == 1
    records = {r["check_point"]: r for r in exec_layer.post_exit_tracker.get_tracking("000001")}
    assert records["T+5"]["quality"] == "correct"
    assert records["T+5"]["current_price"] == 9.0
