"""EntryEngine 单元测试 — 入场逻辑"""
from unittest.mock import MagicMock
from common.database import Database
from execution_layer.core.entry_engine import EntryEngine


def test_spike_defense_rsi_check():
    """Spike defense should include RSI > 70 check (4th check from design doc)"""
    db = Database(":memory:")
    pool = MagicMock()
    pool.get.return_value = {"code": "000001", "status": "pending_entry", "entry_low": 14.0, "entry_high": 16.0, "exit_date": None}

    data_layer = MagicMock()
    class MockKline:
        def __init__(self, close, open_p=0, high=0, low=0):
            self.close = close
            self.open = open_p
            self.high = high
            self.low = low
    # 15 consecutive up days -> RSI = 100
    klines = [MockKline(10 + i * 0.5, open_p=10 + (i - 1) * 0.5) for i in range(15)]
    history = MagicMock()
    history.klines = klines
    data_layer.get_history.return_value = history

    engine = EntryEngine(db, pool, data_layer=data_layer)
    result = engine._check_spike_defense("000001", 15.0)
    assert result is not None
    assert "RSI" in result


def test_spike_defense_rsi_below_70():
    """Spike defense should NOT block when RSI <= 70"""
    db = Database(":memory:")
    pool = MagicMock()
    pool.get.return_value = {"code": "000001", "status": "pending_entry", "entry_low": 14.0, "entry_high": 16.0, "exit_date": None}

    data_layer = MagicMock()
    class MockKline:
        def __init__(self, close, open_p=0, high=0, low=0):
            self.close = close
            self.open = open_p
            self.high = high
            self.low = low
    # 15 flat klines -> RSI = 50
    klines = [MockKline(10.0) for _ in range(15)]
    history = MagicMock()
    history.klines = klines
    data_layer.get_history.return_value = history

    engine = EntryEngine(db, pool, data_layer=data_layer)
    result = engine._check_spike_defense("000001", 10.5)
    assert result is None


def test_spike_defense_insufficient_data():
    """Spike defense should return None when < 5 klines available"""
    db = Database(":memory:")
    pool = MagicMock()
    pool.get.return_value = {"code": "000001", "status": "pending_entry", "entry_low": 14.0, "entry_high": 16.0, "exit_date": None}

    data_layer = MagicMock()
    class MockKline:
        def __init__(self, close):
            self.close = close
    klines = [MockKline(10.0) for _ in range(3)]
    history = MagicMock()
    history.klines = klines
    data_layer.get_history.return_value = history

    engine = EntryEngine(db, pool, data_layer=data_layer)
    result = engine._check_spike_defense("000001", 15.0)
    assert result is None


def test_early_morning_volume_suggestion():
    """09:30-09:35 entries should have volume_suggestion='试探'"""
    db = Database(":memory:")
    pool = MagicMock()
    pool.get.return_value = {"code": "000001", "status": "pending_entry", "entry_low": 14.0, "entry_high": 16.0, "exit_date": None}

    engine = EntryEngine(db, pool, agent_layer=None)
    result = engine.check_entry("000001", 14.0, "09:32:00")
    assert result is not None
    assert result.action == "buy"
    assert result.volume_suggestion == "试探"


def test_early_morning_price_outside_range():
    """09:30-09:35 should return None when price outside entry_low * 1.005"""
    db = Database(":memory:")
    pool = MagicMock()
    pool.get.return_value = {"code": "000001", "status": "pending_entry", "entry_low": 14.0, "entry_high": 16.0, "exit_date": None}

    engine = EntryEngine(db, pool, agent_layer=None)
    result = engine.check_entry("000001", 14.2, "09:32:00")
    assert result is None

def test_spike_defense_check_1_price_above_5day_avg():
    """Verify spike defense check 1: price > 5-day avg * 1.15"""
    from unittest.mock import MagicMock
    from common.database import Database
    db = Database(":memory:")
    pool = MagicMock()
    data = MagicMock()
    class MK:
        def __init__(self, c, o=0): self.close = c; self.open = o
    data.get_history.return_value.klines = [MK(10), MK(10), MK(10), MK(10), MK(10)]
    from execution_layer.core.entry_engine import EntryEngine
    engine = EntryEngine(db, pool, data_layer=data)
    result = engine._check_spike_defense("000001", 12.0)
    assert result is not None
    assert "5日均价" in result
