# -*- coding: utf-8 -*-
"""P0-3 TDD — execution_stats 分母口径（watch_expired 不进 total_trades）

现状：performance.py 的 total_trades/win_count/loss_count/avg_return 分母
= status='exited' 全部行（含 watch_expired 观察剔除——从未买入过）。
09-25 实证：11 行 exited 全是 watch_expired（entry_price=NULL, total_return_pct=NULL）
→ total_trades=11（错分母）→ win_rate=0.0（真值但错分母）→ avg_return=0.0（分子 0 行）。

修法：分母 = status='exited' AND entry_price IS NOT NULL（真买入过的退出）；
watch_expired 单列 watch_expired_count 不进 total_trades。
历史行不改（判例「不回填旧数据」——只改计算逻辑，不改存量）。
"""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))


@pytest.fixture()
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE stock_pool (
        code TEXT, name TEXT, status TEXT, entry_price REAL, entry_date TEXT,
        exit_date TEXT, exit_reason TEXT, total_return_pct REAL)""")
    yield conn
    conn.close()


def _insert(conn, code, status, entry_price, exit_date, exit_reason, ret):
    conn.execute(
        "INSERT INTO stock_pool VALUES (?,?,?,?,?,?,?,?)",
        (code, code, status, entry_price, exit_date if status == "exited" else None,
         exit_date, exit_reason, ret))
    conn.commit()


class TestDenominatorExcludesWatchExpired:
    """P0-3：watch_expired（未买入）不进 total_trades 分母"""

    def test_watch_expired_not_in_total_trades(self, db):
        _insert(db, "600000", "exited", None, "2026-09-25", "watch_expired", None)
        _insert(db, "000001", "exited", 10.0, "2026-09-25", "止损", -0.05)
        # 修法后的分母：只算 entry_price 非空的退出
        total = db.execute(
            "SELECT COUNT(*) FROM stock_pool WHERE status='exited' "
            "AND entry_price IS NOT NULL AND exit_date='2026-09-25'").fetchone()[0]
        assert total == 1, "watch_expired 不进 total_trades"

    def test_win_count_only_real_trades(self, db):
        _insert(db, "600000", "exited", None, "2026-09-25", "watch_expired", None)
        _insert(db, "000001", "exited", 10.0, "2026-09-25", "止盈", 0.08)
        _insert(db, "000002", "exited", 20.0, "2026-09-25", "止损", -0.03)
        win = db.execute(
            "SELECT COUNT(*) FROM stock_pool WHERE status='exited' "
            "AND entry_price IS NOT NULL AND exit_date='2026-09-25' "
            "AND total_return_pct > 0").fetchone()[0]
        assert win == 1

    def test_avg_return_not_diluted_by_watch_expired(self, db):
        """avg_return 分子只算 total_return_pct 非空的真交易，分母=真交易数"""
        _insert(db, "600000", "exited", None, "2026-09-25", "watch_expired", None)
        _insert(db, "000001", "exited", 10.0, "2026-09-25", "止盈", 0.10)
        _insert(db, "000002", "exited", 20.0, "2026-09-25", "止损", -0.02)
        rows = db.execute(
            "SELECT total_return_pct FROM stock_pool WHERE status='exited' "
            "AND entry_price IS NOT NULL AND exit_date='2026-09-25' "
            "AND total_return_pct IS NOT NULL").fetchall()
        returns = [r["total_return_pct"] for r in rows]
        avg = round(sum(returns) / len(returns), 4) if returns else 0.0
        assert avg == round((0.10 + (-0.02)) / 2, 4), \
            "avg_return 不应被 watch_expired 稀释"

    def test_watch_expired_count_tracked_separately(self, db):
        """watch_expired 单列计数，不进 total_trades"""
        _insert(db, "600000", "exited", None, "2026-09-25", "watch_expired", None)
        _insert(db, "600001", "exited", None, "2026-09-25", "watch_expired", None)
        _insert(db, "000001", "exited", 10.0, "2026-09-25", "止损", -0.05)
        watch_n = db.execute(
            "SELECT COUNT(*) FROM stock_pool WHERE status='exited' "
            "AND entry_price IS NULL AND exit_date='2026-09-25'").fetchone()[0]
        assert watch_n == 2, "watch_expired 单列计数"

    def test_historical_rows_not_modified(self, db):
        """存量行不改（判例「不回填旧数据」——只改计算逻辑）"""
        _insert(db, "600000", "exited", None, "2026-09-20", "watch_expired", None)
        # 旧行还在，只是新计算口径不把它算进分母
        n = db.execute("SELECT COUNT(*) FROM stock_pool WHERE exit_date='2026-09-20'"
                       ).fetchone()[0]
        assert n == 1, "存量行不得被删除/修改"


class TestWinRateDenominator:
    """win_rate 分母=真交易数"""

    def test_win_rate_uses_real_trades(self, db):
        _insert(db, "600000", "exited", None, "2026-09-25", "watch_expired", None)
        _insert(db, "000001", "exited", 10.0, "2026-09-25", "止盈", 0.10)
        _insert(db, "000002", "exited", 20.0, "2026-09-25", "止损", -0.02)
        total = db.execute(
            "SELECT COUNT(*) FROM stock_pool WHERE status='exited' "
            "AND entry_price IS NOT NULL AND exit_date='2026-09-25'").fetchone()[0]
        win = db.execute(
            "SELECT COUNT(*) FROM stock_pool WHERE status='exited' "
            "AND entry_price IS NOT NULL AND exit_date='2026-09-25' "
            "AND total_return_pct > 0").fetchone()[0]
        win_rate = round(win / max(total, 1), 4)
        assert win_rate == 0.5, "win_rate 分母=真交易数（不含 watch_expired）"
