"""Opus5 审计批次二回归 — AUD-008 / AUD-006。"""
import logging

import pytest

from common.database import Database
from common.utils import bj_date
from execution_layer.core.stock_pool import StockPool
from execution_layer.stats.performance import ExecutionStats
from execution_layer.tables import (
    ACTION_RECORDS_DDL,
    EXECUTION_STATS_DDL,
    EXIT_TRACKING_DDL,
    STOCK_POOL_DDL,
)


@pytest.fixture
def db():
    database = Database(":memory:")
    conn = database.get_connection()
    for ddl in (STOCK_POOL_DDL, ACTION_RECORDS_DDL, EXIT_TRACKING_DDL, EXECUTION_STATS_DDL):
        conn.executescript(ddl)
    conn.commit()
    yield database
    database.close()


def _prev_month(ym: str) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


class TestUpdateWhitelistVisibility:
    """AUD-008: update() 白名单丢弃必须显形, 不得静默。"""

    def test_unknown_column_is_logged_not_silently_dropped(self, db, caplog):
        pool = StockPool(db, None)
        db.get_connection().execute(
            "INSERT INTO stock_pool (code, name, status, added_date) "
            "VALUES ('000001', 'A', 'active', '2026-08-10')")
        db.get_connection().commit()

        with caplog.at_level(logging.WARNING):
            pool.update("000001", stop_loss=9.5, stop_loss_pct=0.05, bogus_col=1)

        # 合法列照写
        row = db.get_connection().execute(
            "SELECT stop_loss FROM stock_pool WHERE code='000001'").fetchone()
        assert row["stop_loss"] == 9.5
        # 被丢弃的列名必须出现在 WARNING 里 (否则调用方永远不知道写丢了)
        text = caplog.text
        assert "stop_loss_pct" in text, "被白名单丢弃的列名必须显形"
        assert "bogus_col" in text

    def test_all_kwargs_dropped_is_logged(self, db, caplog):
        pool = StockPool(db, None)
        with caplog.at_level(logging.WARNING):
            pool.update("000001", not_a_column=1)
        assert "not_a_column" in caplog.text


class TestMonthlyStatsAreMonthly:
    """AUD-006: get_monthly_stats 必须只统计当月, 不是全历史。"""

    def test_only_current_month_trades_counted(self, db):
        this_month = bj_date()[:7]
        last_month = _prev_month(this_month)
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO stock_pool (code, name, status, added_date, total_return_pct, exit_date) "
            "VALUES ('600001','A','exited','2026-01-05', 1.0, ?),"
            "       ('600002','B','exited','2026-01-05', 2.0, ?),"
            "       ('600003','C','exited','2026-01-05', 3.0, ?),"
            "       ('600004','D','exited','2026-01-05', 4.0, ?),"
            "       ('600005','E','exited','2026-01-05', 5.0, ?)",
            (f"{this_month}-05", f"{this_month}-06",
             f"{last_month}-05", f"{last_month}-06", f"{last_month}-07"))
        conn.commit()

        stats = ExecutionStats(db).get_monthly_stats()
        assert stats["total_trades"] == 2, (
            "月度统计只应含当月了结的 2 笔, 实得 %s (全历史 5 笔=未接月份过滤)"
            % stats["total_trades"])
        assert stats["avg_return"] == pytest.approx(1.5)   # (1.0+2.0)/2
