"""Execution Layer 单元测试"""
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


def test_add_to_watch(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行", "strategy", "测试", 85)
    pool = exec_layer.stock_pool.get("000001")
    assert pool is not None
    assert pool["status"] == "watch"


def test_status_transition(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "pending_entry")
    pool = exec_layer.stock_pool.get("000001")
    assert pool["status"] == "pending_entry"


def test_action_record(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.action_record.write("000001", "add_to_watch", "测试", "manual")
    records = exec_layer.action_record.get_by_code("000001")
    assert len(records) >= 1
    assert records[0]["action_type"] == "add_to_watch"


def test_stock_pool_has_all_columns(exec_layer):
    conn = exec_layer.db.get_connection()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(stock_pool)").fetchall()]
    required = [
        "add_position_count", "reduce_position_count", "partial_exit_count",
        "swap_count", "latest_adjustment_action", "latest_adjustment_date",
        "position_pct_initial",
    ]
    for col in required:
        assert col in cols, f"Missing column: {col}"


def test_portfolio_snapshot(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.add("000002", "万科A", "strategy", "测试", 80)
    exec_layer.stock_pool.update_status("000001", "active")
    snap = exec_layer.get_portfolio_snapshot()
    assert snap.active == 1
    assert snap.watch == 1


def test_get_exit_review(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "exited")
    exec_layer.stock_pool.update("000001", exit_price=15.0, entry_price=10.0, total_return_pct=50.0)
    review = exec_layer.get_exit_review("000001")
    assert review is not None
    assert review.code == "000001"
    assert review.exit_price == 15.0


def test_get_exit_review_not_exited(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    review = exec_layer.get_exit_review("000001")
    assert review is None


def test_get_monthly_stats(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "exited")
    exec_layer.stock_pool.update("000001", total_return_pct=10.0)
    stats = exec_layer.get_monthly_stats()
    assert stats is not None
    assert hasattr(stats, "win_rate")


def test_get_summary_card(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "exited")
    exec_layer.stock_pool.update("000001", total_return_pct=10.0)
    card = exec_layer.get_summary_card()
    assert card is not None
    assert card.total_trades >= 0


def test_get_summary_card_with_data(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "exited")
    exec_layer.stock_pool.update("000001", total_return_pct=5.0)
    card = exec_layer.get_summary_card()
    assert card.total_trades == 1
    assert card.win_rate == 1.0
    assert card.total_return_pct == 5.0
    assert card.active_positions == 0
    assert card.date != ""

    exec_layer.stock_pool.add("000002", "万科A")
    exec_layer.stock_pool.update_status("000002", "exited")
    exec_layer.stock_pool.update("000002", total_return_pct=-3.0)
    card = exec_layer.get_summary_card()
    assert card.total_trades == 2
    assert card.win_rate == 0.5
    assert card.total_return_pct == 1.0  # (5.0 + -3.0) / 2


def test_get_best_worst_trades(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "exited")
    exec_layer.stock_pool.update("000001", total_return_pct=10.0, exit_price=15.0)
    card = exec_layer.get_best_worst_trades()
    assert len(card.best) >= 0
    assert len(card.worst) >= 0


def test_daily_update(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "active")
    exec_layer.pnl_tracker = _MockPnLTracker(exec_layer.pnl_tracker)
    exec_layer.daily_update()
    assert exec_layer.pnl_tracker.daily_update_called


def test_get_position_detail_nonexistent(exec_layer):
    result = exec_layer.get_position_detail("NONEXIST")
    assert result is None


def test_get_position_detail_active(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行", "strategy", "动量突破", 85)
    exec_layer.stock_pool.update_status("000001", "active")
    exec_layer.stock_pool.update("000001", current_score=90.0, entry_price=10.0, last_price=11.0,
                                 unrealized_pnl_pct=10.0, holding_days=5, stop_loss=9.0, take_profit=12.0,
                                 position_pct=20.0)
    detail = exec_layer.get_position_detail("000001")
    assert detail is not None
    assert detail.code == "000001"
    assert detail.name == "平安银行"
    assert detail.entry_price == 10.0
    assert detail.current_price == 11.0
    assert detail.pnl_pct == 10.0
    assert detail.holding_days == 5
    assert detail.stop_loss == 9.0
    assert detail.take_profit == 12.0
    assert detail.score == 90.0
    assert detail.entry_reason == "动量突破"
    assert detail.position_pct == 20.0
    assert "动量突破" in detail.reason_chain
    assert "入场原因" in detail.reason_chain
    assert "当前评分" in detail.reason_chain


def test_get_position_detail_watch(exec_layer):
    exec_layer.stock_pool.add("000002", "万科A", "strategy", "低估值", 75)
    detail = exec_layer.get_position_detail("000002")
    assert detail is not None
    assert "低估值" in detail.reason_chain
    assert "入场原因" in detail.reason_chain


def test_get_position_detail_defaults(exec_layer):
    exec_layer.stock_pool.add("000003", "测试")
    exec_layer.stock_pool.update_status("000003", "active")
    detail = exec_layer.get_position_detail("000003")
    assert detail is not None
    assert "策略推荐" in detail.reason_chain
    assert detail.entry_price == 0.0
    assert detail.score == 0.0


def test_intraday_scan(exec_layer):
    exec_layer.intraday_monitor = _MockIntradayMonitor()
    exec_layer.intraday_scan()
    assert exec_layer.intraday_monitor.scan_called


class _MockPnLTracker:
    def __init__(self, original):
        self.original = original
        self.daily_update_called = False

    def daily_update(self, klines):
        self.daily_update_called = True


class _MockIntradayMonitor:
    def __init__(self):
        self.scan_called = False

    def _scan(self):
        self.scan_called = True

    def stop(self):
        pass
def test_get_best_worst_trades_with_data(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.add("000002", "万科A")
    exec_layer.stock_pool.update_status("000001", "exited")
    exec_layer.stock_pool.update_status("000002", "exited")
    exec_layer.stock_pool.update("000001", total_return_pct=10.0, exit_price=15.0, entry_price=10.0)
    exec_layer.stock_pool.update("000002", total_return_pct=-5.0, exit_price=9.5, entry_price=10.0)
    card = exec_layer.get_best_worst_trades(2)
    assert len(card.best) == 2
    assert len(card.worst) == 2
    assert card.best[0].pnl_pct >= card.best[1].pnl_pct


def test_get_intraday_stats(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "active")
    stats = exec_layer.get_intraday_stats()
    assert stats.active_count == 1
    assert stats.pending_entries == 0


def test_risk_expectation(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "exited")
    exec_layer.stock_pool.update("000001", total_return_pct=-10.0)
    risk = exec_layer.get_risk_expectation()
    assert risk.worst_case_drawdown <= 0
    assert risk.risk_level in ("低", "中", "高")


def test_risk_expectation_no_exited(exec_layer):
    risk = exec_layer.get_risk_expectation()
    assert risk.worst_case_drawdown == 0.0
    assert risk.risk_level in ("低", "中", "高")


def test_daily_price_range_with_data_layer(exec_layer):
    class _MockKline:
        open = 10.0
        high = 11.0
        low = 9.5
        close = 10.5

    class _MockHistory:
        klines = [_MockKline()]

    class _MockDataLayer:
        def get_history(self, code, end_date, window):
            return _MockHistory()

    exec_layer.data_layer = _MockDataLayer()
    result = exec_layer.get_daily_price_range("000001", "2024-01-15")
    assert result is not None
    assert result.code == "000001"
    assert result.open_price == 10.0
    assert result.high_price == 11.0
    assert result.low_price == 9.5
    assert result.close_price == 10.5
    assert result.change_pct == pytest.approx(5.0, rel=1e-3)  # (10.5/10.0 - 1) * 100


def test_daily_price_range_no_data_layer(exec_layer):
    exec_layer.data_layer = None
    result = exec_layer.get_daily_price_range("000001", "2024-01-15")
    assert result is None


def test_daily_price_range_no_history(exec_layer):
    class _MockDataLayer:
        def get_history(self, code, end_date, window):
            return None

    exec_layer.data_layer = _MockDataLayer()
    result = exec_layer.get_daily_price_range("000001", "2024-01-15")
    assert result is None


def test_get_weighted_avg_price_no_records(exec_layer):
    result = exec_layer.get_weighted_avg_price("NONEXIST")
    assert result == 0.0


def test_get_weighted_avg_price(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "active")
    exec_layer.stock_pool.update("000001", entry_price=10.0, position_qty=100)
    result = exec_layer.get_weighted_avg_price("000001")
    assert result == 10.0


def test_get_weighted_avg_price_single_entry(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "active")
    exec_layer.stock_pool.update("000001", entry_price=15.5, position_qty=200)
    result = exec_layer.get_weighted_avg_price("000001")
    assert result == 15.5


def test_stock_pool_get_weighted_avg_price(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "active")
    exec_layer.stock_pool.update("000001", entry_price=8.67, position_qty=100)
    result = exec_layer.stock_pool.get_weighted_avg_price("000001")
    assert result == 8.67


def test_avg_entry_price_in_valid_columns(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update("000001", avg_entry_price=10.5)
    pool = exec_layer.stock_pool.get("000001")
    assert pool is not None
    assert pool["avg_entry_price"] == 10.5


def test_portfolio_snapshot_total_capital_configurable(exec_layer):
    exec_layer.rebalance_engine.total_capital = 500000.0
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "active")
    exec_layer.stock_pool.update("000001", entry_price=10.0, position_qty=100, last_price=11.0)
    snap = exec_layer.get_portfolio_snapshot()
    assert snap.total_capital == 500000.0


def test_risk_expectation_empty_active(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "exited")
    exec_layer.stock_pool.update("000001", total_return_pct=-10.0)
    risk = exec_layer.get_risk_expectation()
    assert risk.current_drawdown == 0.0


def test_start_initializes_tables(exec_layer):
    conn = exec_layer.db.get_connection()
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    names = [r["name"] for r in tables]
    assert "stock_pool" in names
    assert "action_records" in names
    assert "execution_stats" in names


def test_get_today_actions(exec_layer):
    exec_layer.action_record.write("000001", "add_to_watch", "test", "manual")
    feed = exec_layer.get_today_actions()
    assert hasattr(feed, 'actions')


def test_add_position_not_active(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.add_position("000001", 100, 15.0, "test")
    pool = exec_layer.stock_pool.get("000001")
    assert pool["position_qty"] == 0 or pool["position_qty"] is None


def test_partial_exit_100_percent(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "active")
    exec_layer.stock_pool.update("000001", position_qty=100)
    exec_layer.stock_pool.partial_exit("000001", 100, 15.0, "全部退出")
    pool = exec_layer.stock_pool.get("000001")
    assert pool["status"] == "exited"


def test_shutdown(exec_layer):
    result = exec_layer.shutdown()
    assert result is True
    assert exec_layer.intraday_monitor._running is False


def test_get_intraday_pnl_trend(exec_layer):
    trend = exec_layer.intraday_monitor.get_intraday_pnl_trend()
    assert isinstance(trend, list)



def test_check_add_at_max_position(exec_layer):
    for i in range(10):
        code = f"{i:06d}"
        exec_layer.stock_pool.add(code, f"Stock{i}")
        exec_layer.stock_pool.update_status(code, "active")
    result = exec_layer.rebalance_engine.check_add("000001", 15.0, 100000.0)
    assert result.can_add is False
    assert "上限" in result.reason


def test_run_analyzer_unknown(exec_layer):
    result = exec_layer.run_analyzer("nonexistent")
    assert result.detail.get("error") is not None
