"""layer4 最短持有期测试 — 持仓 <3 交易日不触发技术退出

背景: 2026-08-28 模拟盘首日, 08-27 入场的 10 仓次日 09:30 即被 layer4
均线空头排列全灭。技术面退出须给持仓最短持有期 (硬止损等风险层不受限)。
注意: stock_pool.holding_days 是死列 (只读不增恒 0), 持有交易日必须由
entry_date 经 TradingCalendar 推算。
"""
import pytest
from unittest.mock import MagicMock
from common.database import Database
from execution_layer.core.exit_engine import ExitEngine


class MockKline:
    def __init__(self, close):
        self.close = close


@pytest.fixture
def bearish_kline():
    return [MockKline(20 - i * 0.5) for i in range(20)]  # 递减 → 空头排列


def _engine(pool_dict, config=None):
    db = Database(":memory:")
    pool = MagicMock()
    pool.get.return_value = pool_dict
    return ExitEngine(db, pool, agent_layer=None, config=config)


def _base_pool(entry_date):
    return {"code": "000001", "status": "active", "entry_price": 20.0,
            "entry_date": entry_date}


@pytest.fixture
def fake_today(monkeypatch):
    import common.utils as u
    monkeypatch.setattr(u, "bj_date", lambda: "2026-08-28")  # 周五
    return "2026-08-28"


def test_layer4_blocked_within_min_holding(fake_today, bearish_kline):
    """08-27 入场, 08-28 检查 → 持有 1 交易日 <3, layer4 不得触发"""
    engine = _engine(_base_pool("2026-08-27"))
    result = engine.check_exits("000001", 18.0, kline_data=bearish_kline)
    assert result is None, f"持有 1 日不应触发技术退出, got {result and result.reason}"


def test_layer4_fires_after_min_holding(fake_today, bearish_kline):
    """08-21 入场, 08-28 检查 → 持有 5 交易日 >=3, layer4 正常触发"""
    engine = _engine(_base_pool("2026-08-21"))
    result = engine.check_exits("000001", 18.0, kline_data=bearish_kline)
    assert result is not None
    assert result.reason_layer == 4


def test_min_holding_days_configurable(fake_today, bearish_kline):
    """config execution.min_holding_days=10 → 持有 5 日仍被拦"""
    engine = _engine(_base_pool("2026-08-21"),
                     config={"execution.min_holding_days": 10})
    result = engine.check_exits("000001", 18.0, kline_data=bearish_kline)
    assert result is None


def test_hard_stop_not_gated_on_day1(fake_today):
    """最短持有期只限 layer4: 首日硬止损 (layer1) 仍须生效"""
    pool = _base_pool("2026-08-27")
    pool["stop_loss"] = 19.0
    engine = _engine(pool)
    result = engine.check_exits("000001", 18.0, kline_data=None)
    assert result is not None
    assert result.reason_layer == 1


def test_missing_entry_date_fail_open(fake_today, bearish_kline):
    """entry_date 缺失 → 不拦 (保持旧行为, fail-open)"""
    pool = {"code": "000001", "status": "active", "entry_price": 20.0}
    engine = _engine(pool)
    result = engine.check_exits("000001", 18.0, kline_data=bearish_kline)
    assert result is not None
    assert result.reason_layer == 4
