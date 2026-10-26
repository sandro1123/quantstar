from unittest.mock import MagicMock, patch
from common.database import Database
from execution_layer.intraday.monitor import IntradayMonitor
from data_layer._models import IntradaySnapshot


def test_adaptive_interval_opening():
    """Monitor should use 2min interval during opening (09:30-10:00)"""
    db = Database(":memory:")
    pool = MagicMock()
    monitor = IntradayMonitor(db, pool)
    assert monitor._get_interval_for_time("09:35") == 120
    assert monitor._get_interval_for_time("10:30") == 300
    assert monitor._get_interval_for_time("14:40") == 60
    assert monitor._get_interval_for_time("14:45") == 60


def test_adaptive_interval_boundaries():
    """Test boundary conditions for adaptive intervals"""
    db = Database(":memory:")
    pool = MagicMock()
    monitor = IntradayMonitor(db, pool)
    assert monitor._get_interval_for_time("09:30") == 120
    assert monitor._get_interval_for_time("09:59") == 120
    assert monitor._get_interval_for_time("10:00") == 300
    assert monitor._get_interval_for_time("14:29") == 300
    assert monitor._get_interval_for_time("14:30") == 60
    assert monitor._get_interval_for_time("14:59") == 60


def test_adaptive_interval_outside_market():
    """Outside market hours should default to 300s"""
    db = Database(":memory:")
    pool = MagicMock()
    monitor = IntradayMonitor(db, pool)
    assert monitor._get_interval_for_time("08:00") == 300
    assert monitor._get_interval_for_time("15:30") == 300
    assert monitor._get_interval_for_time("00:00") == 300


def test_auction_check():
    """_auction_check should query pending_entry stocks"""
    db = Database(":memory:")
    pool = MagicMock()
    monitor = IntradayMonitor(db, pool)
    pool.get_by_status.return_value = [
        {"code": "000001", "entry_low": 10.0, "entry_high": 11.0},
        {"code": "000002", "entry_low": 20.0, "entry_high": 21.0},
    ]
    monitor._auction_check()
    pool.get_by_status.assert_called_once_with("pending_entry")


def test_post_close_processing():
    """F4.4: _post_close_processing 只标记触发, 不结算 exit_pending"""
    db = Database(":memory:")
    pool = MagicMock()
    monitor = IntradayMonitor(db, pool)
    exit_checker = MagicMock()
    monitor.exit_checker = exit_checker
    monitor.paper_broker.execute_exit = MagicMock(return_value=True)
    active_rows = [
        {"code": "000001", "last_price": 10.5},
        {"code": "000002", "last_price": 20.5},
    ]
    pending_exit_rows = [
        {"code": "000003", "last_price": 5.5},
        {"code": "000004", "last_price": 6.5},
    ]
    pool.get_by_status.side_effect = [active_rows, pending_exit_rows]
    monitor._post_close_processing()
    assert pool.get_by_status.call_count == 2
    assert pool.get_by_status.call_args_list[0] == (("active",),)
    assert pool.get_by_status.call_args_list[1] == (("exit_pending",),)
    assert exit_checker.check.call_count == 2
    # 触发 → 标记; exit_pending → 留给 T+1 结算, 盘中绝不 execute_exit
    assert pool.mark_exit_trigger.call_count == 2
    assert monitor.paper_broker.execute_exit.call_count == 0


def test_scan_does_not_settle_exit_pending():
    """F4.4: _scan 不再盘中结算 exit_pending — 结算唯一入口 = daily_update T+1"""
    db = Database(":memory:")
    pool = MagicMock()
    monitor = IntradayMonitor(db, pool)
    monitor.paper_broker.execute_exit = MagicMock()
    pool.get_by_status.side_effect = [
        [],   # active
        [],   # pending_entry
        [{"code": "000003", "last_price": 5.5, "exit_reason": "止损"}],
    ]
    monitor._scan()
    assert pool.get_by_status.call_count == 3
    assert pool.get_by_status.call_args_list[2] == (("exit_pending",),)
    monitor.paper_broker.execute_exit.assert_not_called()


def test_volatility_check_stock_drop_calls_agent():
    """_check_volatility should call agent when stock drops >3% from entry"""
    db = Database(":memory:")
    pool = MagicMock()
    data_layer = MagicMock()
    agent = MagicMock()
    agent.analyze.return_value = MagicMock(result="个股下跌分析", output="个股下跌分析")
    monitor = IntradayMonitor(db, pool, data_layer=data_layer)
    monitor.agent_layer = agent
    active = [{"code": "000001", "entry_price": 10.0, "last_price": 9.3, "status": "active"}]
    monitor._check_volatility(active)
    agent.analyze.assert_called_once()
    call_args = agent.analyze.call_args[0][0]
    assert call_args.task == "stock_analysis"
    assert call_args.input["code"] == "000001"


def test_volatility_check_no_drop_skips_agent():
    """_check_volatility should not call agent when no drops"""
    db = Database(":memory:")
    pool = MagicMock()
    data_layer = MagicMock()
    snap = IntradaySnapshot(timestamp="2026-07-21 10:00:00")
    data_layer.snapshot_intraday.return_value = snap
    agent = MagicMock()
    monitor = IntradayMonitor(db, pool, data_layer=data_layer)
    monitor.agent_layer = agent
    active = [{"code": "000001", "entry_price": 10.0, "last_price": 10.5, "status": "active"}]
    monitor._check_volatility(active)
    agent.analyze.assert_not_called()


def test_volatility_check_exception_does_not_crash():
    """_check_volatility should not crash on exception"""
    db = Database(":memory:")
    pool = MagicMock()
    data_layer = MagicMock()
    data_layer.snapshot_intraday.side_effect = Exception("DB error")
    agent = MagicMock()
    monitor = IntradayMonitor(db, pool, data_layer=data_layer)
    monitor.agent_layer = agent
    monitor._check_volatility([])
    agent.analyze.assert_not_called()


def test_scan_calls_volatility_check():
    """_scan should call _check_volatility"""
    db = Database(":memory:")
    pool = MagicMock()
    agent = MagicMock()
    monitor = IntradayMonitor(db, pool)
    monitor.agent_layer = agent
    pool.get_by_status.side_effect = lambda s: [{"code": "000001", "entry_price": 10.0, "last_price": 9.3, "status": "active"}] if s == "active" else []
    with patch.object(monitor, '_check_volatility') as mock_check:
        with patch.object(monitor, '_record_pnl_snapshot'):
            monitor._scan()
    mock_check.assert_called_once()
    db = Database(":memory:")
    pool = MagicMock()
    monitor = IntradayMonitor(db, pool)
    with (
        patch.object(monitor, '_get_interval_for_time', return_value=120),
        patch.object(monitor, '_scan') as mock_scan,
        patch.object(monitor, '_post_close_processing'),
        patch("execution_layer.intraday.monitor.bj_now", return_value="2026-07-21 09:35:00"),
        patch("execution_layer.intraday.monitor.time.sleep"),
    ):
        monitor._running = True
        def stop_loop(*args):
            monitor._running = False
        mock_scan.side_effect = stop_loop
        monitor._loop()
        mock_scan.assert_called_once()
        assert monitor.interval_s == 120


def test_loop_post_close():
    """_loop should trigger post-close at 15:00+"""
    db = Database(":memory:")
    pool = MagicMock()
    monitor = IntradayMonitor(db, pool)
    with (
        patch.object(monitor, '_post_close_processing') as mock_post,
        patch("execution_layer.intraday.monitor.bj_now", return_value="2026-07-21 15:00:00"),
        patch("execution_layer.intraday.monitor.time.sleep"),
    ):
        monitor._running = True
        monitor._loop()
        mock_post.assert_called_once()
        assert monitor._running is False

