"""ExitEngine 单元测试 — 技术面退出层"""
import pytest
from unittest.mock import MagicMock
from common.database import Database
from execution_layer.core.exit_engine import ExitEngine, ExitDecision
from execution_layer.intraday.exit_checker import IntradayExitChecker


@pytest.fixture
def sample_kline():
    class MockKline:
        def __init__(self, close):
            self.close = close
            self.open = close
            self.high = close * 1.01
            self.low = close * 0.99
    prices = [20 - i * 0.5 for i in range(20)]
    return [MockKline(p) for p in prices]


def test_technical_exit_layer_requires_kline_data():
    """_layer_4_technical returns None when no kline_data provided"""
    db = Database(":memory:")
    pool = MagicMock()
    pool.get.return_value = {
        "code": "000001", "status": "active", "entry_price": 20.0,
        "entry_date": "2026-07-01",
    }
    engine = ExitEngine(db, pool, agent_layer=None)
    result = engine.check_exits("000001", 18.0, kline_data=None)
    assert result is None


def test_technical_exit_layer_receives_kline_data(sample_kline):
    """_layer_4_technical returns decision when kline_data provided"""
    db = Database(":memory:")
    pool = MagicMock()
    pool.get.return_value = {
        "code": "000001", "status": "active", "entry_price": 20.0,
        "entry_date": "2026-07-01",
    }
    engine = ExitEngine(db, pool, agent_layer=None)
    result = engine.check_exits("000001", 18.0, kline_data=sample_kline)
    assert result is not None
    assert result.reason_layer == 4


def test_technical_exit_layer_with_dict_kline():
    """_layer_4_technical handles dict-style klines (no close attribute)"""
    db = Database(":memory:")
    pool = MagicMock()
    pool.get.return_value = {
        "code": "000001", "status": "active", "entry_price": 20.0,
        "entry_date": "2026-07-01",
    }
    prices = [20 - i * 0.5 for i in range(20)]
    kline_data = [{"close": p} for p in prices]
    engine = ExitEngine(db, pool, agent_layer=None)
    result = engine.check_exits("000001", 18.0, kline_data=kline_data)
    assert result is not None
    assert result.reason_layer == 4


def test_intraday_scan_calls_exit_checker():
    """Verify monitor._scan delegates to exit_checker for active stocks"""
    from unittest.mock import MagicMock
    from common.database import Database
    from execution_layer.intraday.monitor import IntradayMonitor

    db = Database(":memory:")
    pool = MagicMock()
    pool.get_by_status.side_effect = lambda *args, **kwargs: {
        ("active",): [{"code": "000001", "status": "active"}],
        ("pending_entry",): [],
    }.get(args, [])

    exit_checker = MagicMock()
    entry_checker = MagicMock()

    monitor = IntradayMonitor(db, pool)
    monitor.exit_checker = exit_checker
    monitor.entry_checker = entry_checker
    monitor._record_pnl_snapshot = MagicMock()

    monitor._scan()
    exit_checker.check.assert_called_once()


def test_exit_checker_passes_kline_data():
    """Verify IntradayExitChecker passes kline_data to ExitEngine"""
    db = Database(":memory:")
    pool = MagicMock()
    pool.get.return_value = {
        "code": "000001", "status": "active", "entry_price": 20.0,
        "entry_date": "2026-07-01", "holding_days": 5,
        "highest_price": 20.5, "stop_loss": 0, "take_profit": 0,
        "trailing_stop": 0, "atr_stop": 0, "current_score": 0, "initial_score": 0,
    }

    data_layer = MagicMock()

    class MockKline:
        def __init__(self, close, open_p=0):
            self.close = close
            self.open = open_p
    klines = [MockKline(20 - i * 0.5, open_p=20 - i * 0.5) for i in range(20)]
    data_layer.get_history.return_value.klines = klines

    # 插入 kline 数据供 _get_current_price DB 查询
    conn = db.get_connection()
    conn.execute("CREATE TABLE IF NOT EXISTS daily_klines (code TEXT, date TEXT, close REAL, PRIMARY KEY (code, date))")
    conn.execute("INSERT INTO daily_klines (code, date, close) VALUES (?, ?, ?)",
                 ("000001", "2026-07-24", 18.0))
    conn.commit()

    engine = ExitEngine(db, pool, agent_layer=None)
    checker = IntradayExitChecker(db, pool, engine, data_layer=data_layer)

    result = checker.check("000001", {"code": "000001", "status": "active", "entry_price": 20.0, "entry_date": "2026-07-01"})
    assert result is not None, "Should trigger exit with kline_data"
    assert result["action"] == "sell"


def test_technical_layer_returns_none_with_few_klines():
    """_layer_4_technical returns None with < 20 klines"""
    db = Database(":memory:")
    pool = MagicMock()
    pool.get.return_value = {
        "code": "000001", "status": "active", "entry_price": 20.0,
        "entry_date": "2026-07-01",
    }
    kline_data = [MagicMock(close=15.0) for _ in range(5)]
    engine = ExitEngine(db, pool, agent_layer=None)
    result = engine.check_exits("000001", 18.0, kline_data=kline_data)
    assert result is None


# === Task 1.1: Parameter deviation fixes ===

def test_timeout_30_days():
    """Layer 5 timeout should trigger at 30 days (design doc), not 20"""
    db = Database(":memory:")
    pool = MagicMock()
    pool.get.return_value = {
        "code": "000001", "status": "active", "entry_price": 15.0,
        "entry_date": "2026-06-01", "holding_days": 25,
    }
    pool.get_by_status.return_value = []
    engine = ExitEngine(db, pool, agent_layer=None)
    result = engine._layer_5_timeout(pool.get.return_value, 14.0)
    assert result is None, "Should not trigger at 25 days"

    pool.get.return_value["holding_days"] = 30
    result = engine._layer_5_timeout(pool.get.return_value, 14.0)
    assert result is not None, "Should trigger at 30 days"
    assert result.reason_layer == 5


def test_score_decay_30_percent():
    """Layer 6 score decay should trigger at 30% drop (design doc), not 50%"""
    db = Database(":memory:")
    pool = MagicMock()
    pool.get.return_value = {
        "code": "000001", "status": "active", "entry_price": 15.0,
        "entry_date": "2026-06-01", "initial_score": 100.0,
        "current_score": 60.0,
    }
    engine = ExitEngine(db, pool, agent_layer=None)
    result = engine._layer_6_score_decay(pool.get.return_value, 14.0)
    assert result is not None, "Should trigger at 40% drop"
    assert result.reason_layer == 6

    pool.get.return_value["current_score"] = 75.0
    result = engine._layer_6_score_decay(pool.get.return_value, 14.0)
    assert result is None, "Should not trigger at 25% drop"


def test_atr_stop_2x_database_path():
    """Layer 2 ATR should use 2x ATR via real database path"""
    from common.database import Database
    db = Database(":memory:")
    from execution_layer.core.stock_pool import StockPool
    pool = StockPool(db)
    pool.add("000001", "test")
    pool.update_status("000001", "active")
    pool.update("000001", entry_price=15.0, entry_date="2026-07-01", atr_stop=2.0)
    from execution_layer.core.exit_engine import ExitEngine
    engine = ExitEngine(db, pool, agent_layer=None)
    result = engine._layer_2_atr_stop(pool.get("000001"), 14.3)
    # 2x ATR: atr_price = 15.0 * (1 - 2 * 2.0 / 100) = 14.40
    # current_price = 14.3 <= 14.40, should trigger
    assert result is not None
    assert result.reason_layer == 2


def test_value_saved_message_in_exit_decision():
    """ExitDecision should include value_saved_message field"""
    decision = ExitDecision(
        action="exit_pending", reason_layer=1,
        reason="test", reason_user="test",
        exit_low=14.0, exit_high=15.0,
        value_saved_message="-7.0%",
    )
    assert decision.value_saved_message == "-7.0%"


def test_rsi_overbought_exit():
    """Layer 4 should trigger on RSI > 75 (overbought)"""
    db = Database(":memory:")
    pool = MagicMock()
    pool.get.return_value = {
        "code": "000001", "status": "active", "entry_price": 20.0,
        "entry_date": "2026-06-01",
    }
    engine = ExitEngine(db, pool, agent_layer=None)

    # 25 klines: flat first 10, then sharp uptrend → RSI > 75
    # MA5/MA10/MA20 not in death cross, no 3-day consecutive decline
    class MockKline:
        def __init__(self, close):
            self.close = close
    prices = [10] * 10 + [10, 10.5, 11, 11.5, 12, 12.5, 12.2, 12.8, 13.2, 13.5, 13.8, 14.0, 14.3, 14.5, 14.8]
    kline_data = [MockKline(p) for p in prices]

    result = engine._layer_4_technical(pool.get.return_value, 15.0, kline_data)
    assert result is not None, "RSI > 75 should trigger exit"
    assert result.reason_layer == 4
    assert "RSI" in result.reason or "RSI" in result.reason_user


def test_macd_death_cross_exit():
    """Layer 4 should trigger on MACD death cross"""
    db = Database(":memory:")
    pool = MagicMock()
    pool.get.return_value = {
        "code": "000001", "status": "active", "entry_price": 20.0,
        "entry_date": "2026-06-01",
    }
    engine = ExitEngine(db, pool, agent_layer=None)

    # 36 klines: flat 17, uptrend 11, then choppy decline 8
    # MA5 > MA10 > MA20 (no death cross), no 3-consecutive decline,
    # but MACD crosses below signal
    class MockKline:
        def __init__(self, close):
            self.close = close
    prices = [10] * 17 + [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20] + [18, 19, 16, 17, 14, 15, 12, 13]
    kline_data = [MockKline(p) for p in prices]

    result = engine._layer_4_technical(pool.get.return_value, 15.0, kline_data)
    assert result is not None, "MACD death cross should trigger exit"
    assert result.reason_layer == 4
    assert "MACD" in result.reason, f"Expected MACD in reason, got: {result.reason}"