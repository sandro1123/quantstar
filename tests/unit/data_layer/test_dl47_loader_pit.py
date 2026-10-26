"""DL-47: DataLoader._get_financials_range 按公告日过滤 (PIT 真实现)

因子经 engine.build_context -> data_layer.get_history -> _get_financials_range
消费财报。该路径此前只按报告期 date 过滤 → 前视偏差。本测试断言:
给定 end_date (as_of), 不返回公告日晚于该日的财报行; 公告日为空的行保留 (诚实降级)。
"""
import pytest
from common.database import Database
from data_layer._loader import DataLoader


@pytest.fixture
def db():
    database = Database(":memory:")
    database.init_db()
    conn = database.get_connection()
    conn.execute("DROP TABLE IF EXISTS stock_financials")
    conn.execute(
        "CREATE TABLE stock_financials ("
        "code TEXT, date TEXT, eps REAL, roe REAL, revenue REAL, profit REAL, "
        "total_assets REAL, total_liabilities REAL, net_assets REAL, "
        "operating_cash_flow REAL, gross_margin REAL, net_margin REAL, "
        "announce_date TEXT DEFAULT '', caliber TEXT DEFAULT 'cumulative')"
    )
    # Q1 报告期 2026-03-31, 公告日 2026-04-24
    # Q2 报告期 2026-06-30, 公告日 2026-08-14
    conn.execute(
        "INSERT INTO stock_financials (code, date, eps, roe, announce_date) VALUES "
        "('000001.SZ', '2026-03-31', 0.50, 10.0, '2026-04-24'), "
        "('000001.SZ', '2026-06-30', 1.20, 12.5, '2026-08-14')"
    )
    conn.commit()
    yield database
    database.close()


def test_loader_financials_excludes_unannounced(db):
    """as_of=2026-07-17: Q2 (公告 08-14) 未公告 → 只返回 Q1。"""
    loader = DataLoader(db)
    conn = db.get_connection()
    rows = loader._get_financials_range(conn, "000001.SZ", "2026-01-01", "2026-07-17")
    dates = [r.date for r in rows]
    assert "2026-03-31" in dates
    assert "2026-06-30" not in dates, "公告日未到的财报不得进入因子上下文 (前视偏差)"


def test_loader_financials_includes_after_announce(db):
    """as_of=2026-08-20: Q2 已公告 (08-14) → 返回 Q1+Q2。"""
    loader = DataLoader(db)
    conn = db.get_connection()
    rows = loader._get_financials_range(conn, "000001.SZ", "2026-01-01", "2026-08-20")
    dates = [r.date for r in rows]
    assert "2026-03-31" in dates
    assert "2026-06-30" in dates


def test_loader_financials_empty_announce_degrades(db):
    """公告日为空 (未回补) → 保留该行, 诚实降级为旧行为。"""
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO stock_financials (code, date, eps, announce_date) VALUES "
        "('600000.SH', '2026-03-31', 0.80, '')"
    )
    conn.commit()
    loader = DataLoader(db)
    rows = loader._get_financials_range(conn, "600000.SH", "2026-01-01", "2026-07-01")
    assert any(r.date == "2026-03-31" for r in rows), "公告日为空的行应保留 (降级)"


def test_loader_get_history_pit(db):
    """get_history 端到端: as_of=2026-07-17 时 financials 不含未公告的 Q2。"""
    loader = DataLoader(db)
    hw = loader.get_history("000001.SZ", "2026-07-17", window=30)
    dates = [f.date for f in hw.financials]
    assert "2026-06-30" not in dates
