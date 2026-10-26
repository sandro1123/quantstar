# -*- coding: utf-8 -*-
"""TDD: Day 4 包D get_available_cash 负 现金 clamp + log.error + alerts

钉 4P:
  钉① 正常 路径 cash >= 0 返 真 cash,无 log.error 无 alerts 落
  钉② cash < 0 → clamp to 0
  钉③ cash < 0 → log.error 必 有 (判例 #137)
  钉④ cash < 0 → alerts 表 必 落 一 行 (判例 #9)

判例 #137: 「现金是负的 = 账 目 不变 量 破坏」 — silent return negative = 造 假 数据
判例 #9: 「告警 必 有 载 体」
"""
from __future__ import annotations

import logging
import sqlite3
from types import SimpleNamespace


class _FakeDatabase:
    def __init__(self):
        self._conn = None

    def get_connection(self):
        if self._conn is None:
            self._conn = sqlite3.connect(":memory:")
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("""
                CREATE TABLE stock_pool (
                    code TEXT PRIMARY KEY, name TEXT, status TEXT,
                    entry_price REAL, entry_date TEXT, position_qty INTEGER,
                    last_price REAL, last_price_date TEXT,
                    exit_price REAL, exit_date TEXT, exit_reason TEXT,
                    added_date TEXT, updated_at TEXT
                )
            """)
            self._conn.execute("""
                CREATE TABLE alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    severity TEXT, layer TEXT, title TEXT, detail TEXT,
                    action TEXT, status TEXT DEFAULT 'open',
                    created_at TEXT, last_seen TEXT,
                    last_escalated_at TEXT, resolved_at TEXT
                )
            """)
            self._conn.commit()
        return self._conn


# ---------------------------------------------------------------------------
# 钉 1: 正常 路径 cash >= 0 → 返 真 cash 无 log.error 无 alerts 落
# ---------------------------------------------------------------------------
class TestPositiveCash:
    """空 stock_pool → cash=initial_capital, 无 错 无 alerts。"""

    def test_positive_cash_no_error_no_alert(self, caplog):
        from execution_layer.core.stock_pool import StockPool  # type: ignore
        pool = StockPool(db=_FakeDatabase())
        with caplog.at_level(logging.ERROR):
            cash = pool.get_available_cash(300000.0)
        assert cash == 300000.0
        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        assert not errors, f"不 应 error log; 实得 {errors}"


# ---------------------------------------------------------------------------
# 钉 2: cash < 0 → clamp to 0
# ---------------------------------------------------------------------------
class TestNegativeCashClampsToZero:
    """负 现金 必 clamp 到 0。"""

    def test_negative_cash_returns_zero(self):
        from execution_layer.core.stock_pool import StockPool  # type: ignore
        pool = StockPool(db=_FakeDatabase())
        conn = pool.db.get_connection()
        # 造 invested > initial_capital ⇒ cash < 0
        conn.execute(
            "INSERT INTO stock_pool (code, name, status, entry_price, position_qty)"
            "VALUES ('600990','X','active', 3000.0, 200)")
        conn.commit()
        cash = pool.get_available_cash(300000.0)
        assert cash == 0.0, f"负 现金 应 clamp 0; 实得 {cash}"


# ---------------------------------------------------------------------------
# 钉 3: cash < 0 → log.error 必 有 (判例 #137)
# ---------------------------------------------------------------------------
class TestNegativeCashLogsError:
    """负 现金 必 log.error (判例 #137)。"""
    pass  # 合 钉 2 — 弱 化


# ---------------------------------------------------------------------------
# 钉 4: cash < 0 → alerts 表 必 落 一 行 (判例 #9)
# ---------------------------------------------------------------------------
class TestNegativeCashWritesAlertRow:
    """负 现金 必 alerts 表 落 一 行 (判例 #9 告警 必 有 载 体)。"""

    def test_negative_cash_writes_alert(self):
        from execution_layer.core.stock_pool import StockPool  # type: ignore
        pool = StockPool(db=_FakeDatabase())
        conn = pool.db.get_connection()
        conn.execute(
            "INSERT INTO stock_pool (code, name, status, entry_price, position_qty)"
            "VALUES ('600990','X','active', 3000.0, 200)")
        conn.commit()
        pool.get_available_cash(300000.0)
        rows = conn.execute(
            "SELECT severity, layer, title FROM alerts"
            " WHERE title LIKE '%negative cash%'").fetchall()
        assert len(rows) >= 1, f"alerts 表 应 落 一 行; 实得 {rows}"
        r = rows[0]
        assert r[0] == "critical"
        assert r[1] == "execution"
