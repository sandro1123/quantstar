"""BT-CORRECTNESS ①: 纸交易默认 T+1 开盘成交 — run_daily(date) 消费上一交易日信号。"""
import sqlite3
from datetime import date
from types import SimpleNamespace

import pytest

from backtest_layer.paper_trading.runner import PaperTradingRunner
from execution_layer.tables import init_execution_tables

SIGNAL_DAY = date(2026, 8, 19).isoformat()   # Wed
FILL_DAY = date(2026, 8, 20).isoformat()     # Thu


@pytest.fixture(autouse=True)
def _mute(monkeypatch):
    from execution_layer.core.paper_broker import PaperBroker
    monkeypatch.setattr(PaperBroker, "_send_notification", lambda self, t, b: None)


class _Db:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        init_execution_tables(self.conn)
        self.conn.execute(
            "INSERT INTO stock_pool (code, name, status, source, added_date, entry_low, entry_high) "
            "VALUES ('000001', '平安银行', 'watch', 'strategy', ?, 9.8, 10.2)", (SIGNAL_DAY,))
        self.conn.commit()

    def get_connection(self):
        return self.conn


class _DL:
    def __init__(self, bars):
        self.bars = bars  # {date: (open, close)}

    def snapshot(self, d):
        if d not in self.bars:
            return SimpleNamespace(klines=[])
        o, c = self.bars[d]
        return SimpleNamespace(klines=[SimpleNamespace(code="000001", open=o, close=c)])


def test_next_open_is_default_and_fills_next_day_open():
    db = _Db()
    dl = _DL({SIGNAL_DAY: (9.9, 10.0), FILL_DAY: (10.3, 10.8)})
    runner = PaperTradingRunner(db=db, data_layer=dl)
    assert runner.fill_mode() == "next_open"
    assert runner.signal_date_for(FILL_DAY) == SIGNAL_DAY
    res = runner.run_daily(FILL_DAY)
    assert res.n_signals == 1 and res.n_filled == 1
    assert res.slippages == [pytest.approx(0.03)]  # 开盘 10.3 vs 信号价 10.0
    row = db.conn.execute("SELECT entry_price FROM stock_pool WHERE code='000001'").fetchone()
    assert row["entry_price"] == pytest.approx(10.3)


def test_next_open_signal_day_run_does_not_fill_same_close():
    db = _Db()
    runner = PaperTradingRunner(db=db, data_layer=_DL({SIGNAL_DAY: (9.9, 10.0)}))
    res = runner.run_daily(SIGNAL_DAY)  # 消费的是前一交易日信号 → 无
    assert res.n_signals == 0 and res.n_filled == 0


def test_same_close_legacy_configurable():
    db = _Db()
    runner = PaperTradingRunner(db=db, data_layer=_DL({SIGNAL_DAY: (9.9, 10.5)}),
                                config=SimpleNamespace(get=lambda k, d=None: {
                                    "backtest.paper_trading.fill_price": "same_close",
                                    "execution.dry_run": False}.get(k, d)))
    res = runner.run_daily(SIGNAL_DAY)
    assert res.n_filled == 1
    assert res.slippages == [pytest.approx(0.05)]
