"""S2 F2.1 — DataLayer.health() 多表新鲜度三色判定回归测试"""
from datetime import datetime, timedelta

from common.database import Database
from common.utils import bj_date
from data_layer import DataLayer

TABLES = [
    "daily_klines", "factor_daily", "model_predictions", "stock_valuation",
    "stock_moneyflow", "stock_limit_up_down", "lhb_daily", "concept_daily",
    "analyst_forecasts", "stock_financials", "market_sentiment",
]


def _make_db():
    db = Database(":memory:")
    db.init_db()
    conn = db.get_connection()
    for t in TABLES:
        try:
            conn.execute(f"CREATE TABLE IF NOT EXISTS {t} (code TEXT, date TEXT, x REAL, PRIMARY KEY(code, date))")
        except Exception:
            pass
    conn.commit()
    return db


def _insert(db, table, date_str, code="000001"):
    conn = db.get_connection()
    conn.execute(f"INSERT OR REPLACE INTO {table} (code, date, x) VALUES (?, ?, 1)", (code, date_str))
    conn.commit()


def _health_status(dl):
    h = dl.health()
    return h.status, {c.name: c.status for c in h.checks}


def test_all_tables_green_when_today(db_none=None):
    db = _make_db()
    today = bj_date()
    for t in TABLES:
        _insert(db, t, today)
    dl = DataLayer(db)
    status, checks = _health_status(dl)
    assert status == "green"
    for t in TABLES:
        assert checks[f"freshness_{t}"] == "pass", f"{t} 应 green: {checks[f'freshness_{t}']}"


def test_single_table_red_makes_overall_red(monkeypatch):
    # 时钟钉同 warn: 真交易日 09-30 重演 (假日 10-04 会撞 holiday 分支)。
    from unittest import mock
    import common.utils as _utils
    monkeypatch.setattr(_utils, "bj_date", lambda: "2026-09-30")
    with mock.patch.object(_utils, "bj_date", return_value="2026-09-30"):
        db = _make_db()
        today = "2026-09-30"  # 本地绑定钉死 (顶层 from-import 不受 mock 影响)
    for t in TABLES:
        _insert(db, t, today)
    old = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=40)).strftime("%Y-%m-%d")
    conn = db.get_connection()
    conn.execute("DELETE FROM daily_klines")
    conn.execute("INSERT INTO daily_klines (code, date, x) VALUES ('000002', ?, 1)", (old,))
    conn.commit()
    dl = DataLayer(db)
    status, checks = _health_status(dl)
    assert status == "red"
    assert checks["freshness_daily_klines"] == "fail"
    assert checks["freshness_factor_daily"] == "pass"


def test_warn_threshold_per_table(monkeypatch):
    # 时钟钉 (2026-10-04 假日逼出): 本测原语义=真交易日 warn 场景重演 —
    # 真时钟 10-04 是假日会撞 holiday 分支假绿。钉到 09-30 (真交易日)。
    from unittest import mock
    import common.utils as _utils
    monkeypatch.setattr(_utils, "bj_date", lambda: "2026-09-30")
    with mock.patch.object(_utils, "bj_date", return_value="2026-09-30"):
        db = _make_db()
        today = "2026-09-30"  # 本地绑定钉死 (顶层 from-import 不受 mock 影响)
    for t in TABLES:
        _insert(db, t, today)
    warn_date = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=2)).strftime("%Y-%m-%d")
    conn = db.get_connection()
    conn.execute("DELETE FROM daily_klines")
    conn.execute("INSERT INTO daily_klines (code, date, x) VALUES ('000002', ?, 1)", (warn_date,))
    conn.commit()
    dl = DataLayer(db)
    status, checks = _health_status(dl)
    assert status == "yellow"
    assert checks["freshness_daily_klines"] == "warn"


def test_alpha_factors_not_probed():
    """alpha_factors 为遗留审计缓存 (无实时写入方, S3 豁免), 不纳入新鲜度探针。

    实时管线消费 factor_daily.factor_scores (严格 1/3/5 探针)。若探针包含
    alpha_factors, 其陈旧数据会造成永久 data=red 并触发 F6.3 health_red 误告警。
    """
    db = _make_db()
    today = bj_date()
    for t in TABLES:
        _insert(db, t, today)
    dl = DataLayer(db)
    status, checks = _health_status(dl)
    assert "freshness_alpha_factors" not in checks
    assert status == "green"


def test_empty_table_marked_fail():
    db = _make_db()
    today = bj_date()
    for t in TABLES:
        if t != "market_sentiment":
            _insert(db, t, today)
    dl = DataLayer(db)
    status, checks = _health_status(dl)
    assert status == "red"
    assert checks["freshness_market_sentiment"] == "fail"
