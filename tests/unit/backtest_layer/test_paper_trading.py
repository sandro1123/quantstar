"""BT-12 bridge 单测 (D-24): PaperTradingBridge = 执行层唯一账本的只读视图。

测试用临时库 + 执行层真实 DDL (init_execution_tables) 构造账本场景;
bridge 只读, 不写库、无内存资金。
"""
import sqlite3

import pytest

from backtest_layer.paper_trading.bridge import LedgerPosition, PaperTradingBridge
from execution_layer.tables import init_execution_tables


class _Db:
    """临时库 (内存 sqlite, Row factory)"""

    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row

    def get_connection(self):
        return self.conn


class FakeConfig:
    def __init__(self, values: dict):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)


def _ledger_db(rows: list[dict]) -> _Db:
    """建执行层账本表并插入 stock_pool 行"""
    db = _Db()
    init_execution_tables(db.conn)
    for r in rows:
        db.conn.execute(
            "INSERT INTO stock_pool (code, name, status, source, added_date, "
            "entry_price, position_qty, last_price, unrealized_pnl, total_return_pct) "
            "VALUES (?, ?, ?, 'strategy', '2026-08-19', ?, ?, ?, ?, ?)",
            (r["code"], r.get("name", r["code"]), r["status"],
             r.get("entry_price"), r.get("position_qty", 0),
             r.get("last_price"), r.get("unrealized_pnl"), r.get("total_return_pct")),
        )
    db.conn.commit()
    return db


class TestInitialCapital:
    def test_default(self):
        assert PaperTradingBridge().initial_capital == 300000.0

    def test_from_config(self):
        bridge = PaperTradingBridge(config=FakeConfig({"execution.initial_capital": 500000.0}))
        assert bridge.initial_capital == 500000.0


class TestPositions:
    def test_reads_active_from_ledger(self):
        db = _ledger_db([
            {"code": "000001", "status": "active", "entry_price": 10.0,
             "position_qty": 1000, "last_price": 11.0, "unrealized_pnl": 1000.0},
            {"code": "000002", "status": "exited", "entry_price": 20.0, "position_qty": 500},
        ])
        pos = PaperTradingBridge(db=db).positions()
        assert len(pos) == 1
        assert isinstance(pos[0], LedgerPosition)
        assert pos[0].code == "000001"
        assert pos[0].qty == 1000
        assert pos[0].entry_price == 10.0
        assert pos[0].last_price == 11.0
        assert pos[0].unrealized_pnl == 1000.0

    def test_no_db_returns_empty(self):
        assert PaperTradingBridge().positions() == []

    def test_missing_table_returns_empty(self):
        assert PaperTradingBridge(db=_Db()).positions() == []


class TestCash:
    def test_matches_execution_layer_formula(self):
        # 现金 = 初始资金 − 在投(active entry_price×qty) + 退出回款(exited exit_price×qty)
        db = _ledger_db([
            {"code": "000001", "status": "active", "entry_price": 10.0, "position_qty": 1000},
        ])
        db.conn.execute(
            "INSERT INTO stock_pool (code, name, status, source, added_date, "
            "exit_price, position_qty) VALUES ('000002', 'B', 'exited', 'strategy', "
            "'2026-08-19', 25.0, 400)")
        db.conn.commit()
        bridge = PaperTradingBridge(db=db)
        assert bridge.cash() == pytest.approx(300000.0 - 10000.0 + 10000.0)

    def test_no_db_returns_initial_capital(self):
        assert PaperTradingBridge().cash() == 300000.0


class TestPortfolioValue:
    def test_cash_plus_positions_at_last_price(self):
        db = _ledger_db([
            {"code": "000001", "status": "active", "entry_price": 10.0,
             "position_qty": 1000, "last_price": 11.0},
        ])
        bridge = PaperTradingBridge(db=db)
        # 现金 290000 + 持仓 11×1000
        assert bridge.portfolio_value() == pytest.approx(301000.0)

    def test_current_prices_override(self):
        db = _ledger_db([
            {"code": "000001", "status": "active", "entry_price": 10.0,
             "position_qty": 1000, "last_price": 11.0},
        ])
        bridge = PaperTradingBridge(db=db)
        assert bridge.portfolio_value({"000001": 12.0}) == pytest.approx(302000.0)

    def test_empty_ledger_equals_initial_capital(self):
        bridge = PaperTradingBridge(db=_ledger_db([]))
        assert bridge.portfolio_value() == pytest.approx(300000.0)


class TestUnrealizedPnl:
    def test_from_ledger_positions(self):
        db = _ledger_db([
            {"code": "000001", "status": "active", "entry_price": 10.0,
             "position_qty": 1000, "last_price": 11.0},
            {"code": "000002", "status": "active", "entry_price": 20.0,
             "position_qty": 500, "last_price": 19.0},
        ])
        bridge = PaperTradingBridge(db=db)
        assert bridge.unrealized_pnl() == pytest.approx(1000.0 - 500.0)

    def test_empty_ledger_zero(self):
        assert PaperTradingBridge(db=_ledger_db([])).unrealized_pnl() == 0.0


class TestRealizedPnl:
    def test_from_exited_rows(self):
        db = _ledger_db([
            {"code": "000001", "status": "exited", "entry_price": 10.0,
             "position_qty": 1000, "total_return_pct": 5.0},
            {"code": "000002", "status": "exited", "entry_price": 20.0,
             "position_qty": 500, "total_return_pct": -2.0},
        ])
        bridge = PaperTradingBridge(db=db)
        # 10000×5% − 10000×2% = 300
        assert bridge.realized_pnl() == pytest.approx(300.0)

    def test_empty_ledger_zero(self):
        assert PaperTradingBridge(db=_ledger_db([])).realized_pnl() == 0.0


class TestFills:
    def test_reads_entry_exit_from_action_records(self):
        db = _ledger_db([])
        db.conn.execute(
            "INSERT INTO action_records (code, action_type, price, reason, source) "
            "VALUES ('000001', 'entry', 10.5, 'BT-12', 'execution')")
        db.conn.execute(
            "INSERT INTO action_records (code, action_type, price, reason, source) "
            "VALUES ('000001', 'exit', 11.0, 'BT-12', 'execution')")
        db.conn.execute(
            "INSERT INTO action_records (code, action_type, price, reason, source) "
            "VALUES ('000002', 'add_to_watch', NULL, 'x', 'strategy')")
        db.conn.commit()
        fills = PaperTradingBridge(db=db).fills()
        assert len(fills) == 2
        assert {f["action_type"] for f in fills} == {"entry", "exit"}

    def test_date_filter(self):
        db = _ledger_db([])
        db.conn.execute(
            "INSERT INTO action_records (code, action_type, price, reason, source, created_at) "
            "VALUES ('000001', 'entry', 10.5, 'x', 'execution', '2026-08-19 10:00:00')")
        db.conn.execute(
            "INSERT INTO action_records (code, action_type, price, reason, source, created_at) "
            "VALUES ('000002', 'entry', 20.5, 'x', 'execution', '2026-08-18 10:00:00')")
        db.conn.commit()
        fills = PaperTradingBridge(db=db).fills("2026-08-19")
        assert len(fills) == 1
        assert fills[0]["code"] == "000001"

    def test_no_db_returns_empty(self):
        assert PaperTradingBridge().fills() == []


class TestSummary:
    def test_read_only_snapshot(self):
        db = _ledger_db([
            {"code": "000001", "status": "active", "entry_price": 10.0,
             "position_qty": 1000, "last_price": 11.0},
        ])
        s = PaperTradingBridge(db=db).summary()
        assert s["initial_capital"] == 300000.0
        assert s["cash"] == pytest.approx(290000.0)
        assert s["positions"] == 1
        assert s["portfolio_value"] == pytest.approx(301000.0)
        assert s["unrealized_pnl"] == pytest.approx(1000.0)
        assert s["return_pct"] == pytest.approx(1 / 3, rel=1e-2)

    def test_empty_ledger(self):
        s = PaperTradingBridge(db=_ledger_db([])).summary()
        assert s["portfolio_value"] == pytest.approx(300000.0)
        assert s["return_pct"] == 0.0
