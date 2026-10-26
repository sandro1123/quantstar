"""DL-12: 数据质量基础设施 — freshness_audit 管线 + 读 API

(quality_checks 表与 quality_check 管线已在 DL-11 落地, schema 复用。)
mock + :memory: 库, 无网络无生产库。
"""

import sqlite3

from data_layer.pipelines.base import PipelineInput
from data_layer.pipelines.freshness_audit import (
    FreshnessAuditPipeline, _days_behind,
)


class _FakeDB:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

    def get_connection(self):
        return self.conn


def _mk_db():
    """DataReadiness.REQUIRED_TABLES 全部须存在 (check() 无缺表兜底)"""
    db = _FakeDB()
    c = db.conn
    c.execute("CREATE TABLE daily_klines (code TEXT, date TEXT, "
              "PRIMARY KEY (code, date))")
    c.execute("CREATE TABLE stock_info (code TEXT PRIMARY KEY, name TEXT)")
    c.execute("CREATE TABLE stock_financials (code TEXT, date TEXT)")
    c.execute("CREATE TABLE stock_valuation (code TEXT, date TEXT)")
    c.commit()
    return db


# --- _days_behind ---

def test_days_behind_same_day():
    assert _days_behind("2026-08-18", "2026-08-18") == 0

def test_days_behind_late():
    assert _days_behind("2026-08-18", "2026-08-15") == 3

def test_days_behind_future_clamped():
    assert _days_behind("2026-08-18", "2026-08-19") == 0

def test_days_behind_none():
    assert _days_behind("2026-08-18", None) is None
    assert _days_behind("2026-08-18", "") is None
    assert _days_behind("2026-08-18", "garbage") is None


# --- FreshnessAuditPipeline ---

def test_freshness_audit_creates_table():
    db = _mk_db()
    pipe = FreshnessAuditPipeline(db=db)
    pipe.run(PipelineInput(date="2026-08-18"))
    tables = {r[0] for r in db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "freshness_audit" in tables

def test_freshness_audit_ok_delayed_missing():
    db = _mk_db()
    c = db.conn
    # daily_klines 当日到位 → ok
    c.execute("INSERT INTO daily_klines VALUES ('000001', '2026-08-18')")
    # stock_info 有行无日期维度 → ok (静态表)
    c.execute("INSERT INTO stock_info VALUES ('000001', '平安银行')")
    # stock_valuation 滞后 1 天 → delayed
    c.execute("INSERT INTO stock_valuation VALUES ('000001', '2026-08-17')")
    # stock_financials 空 → missing
    c.commit()
    pipe = FreshnessAuditPipeline(db=db)
    result = pipe.run(PipelineInput(date="2026-08-18"))
    assert result.status == "success"
    rows = {r["table_name"]: r for r in c.execute(
        "SELECT * FROM freshness_audit WHERE date='2026-08-18'")}
    assert rows["daily_klines"]["status"] == "ok"
    assert rows["stock_info"]["status"] == "ok"
    assert rows["stock_valuation"]["status"] == "delayed"
    assert rows["stock_valuation"]["delay_min"] == 1440
    assert rows["stock_financials"]["status"] == "missing"
    assert rows["stock_financials"]["actual_at"] is None

def test_freshness_audit_collect_tables_missing():
    """冷采集表未首跑 → 全部 missing (不影响主管线)"""
    db = _mk_db()
    pipe = FreshnessAuditPipeline(db=db)
    pipe.run(PipelineInput(date="2026-08-18"))
    n_collect = db.conn.execute(
        "SELECT COUNT(*) FROM freshness_audit WHERE table_name IN "
        "('limit_up_ladder','limit_break_pool','hot_stock_history',"
        "'skyrocket_list','stock_anomaly_reasons','sector_fund_flow',"
        "'auction_benchmark','auction_snapshot','lhb_daily')").fetchone()[0]
    assert n_collect == 9  # #116 面一: +lhb_daily → 8→9

def test_freshness_audit_idempotent():
    db = _mk_db()
    pipe = FreshnessAuditPipeline(db=db)
    pipe.run(PipelineInput(date="2026-08-18"))
    n1 = db.conn.execute("SELECT COUNT(*) FROM freshness_audit").fetchone()[0]
    pipe.run(PipelineInput(date="2026-08-18"))
    n2 = db.conn.execute("SELECT COUNT(*) FROM freshness_audit").fetchone()[0]
    # 哨兵④ (09-05) + #116 面一: 公告键与 lhb_daily 双增
    # 4 必需表 + 9 冷采集表 (#116 +lhb_daily) + 1 公告键 (哨兵④)
    assert n1 == n2 == 15

def test_freshness_audit_metadata():
    db = _mk_db()
    pipe = FreshnessAuditPipeline(db=db)
    result = pipe.run(PipelineInput(date="2026-08-18"))
    # 哨兵④ (09-05) + #116 面一 + AUD-014a: 4 必需 + 10 冷采集
    # (含 lhb_daily #116 / index_daily AUD-014a) + 1 公告键
    assert result.metadata["tables_audited"] == 15
    assert result.records_written == 15  # 与 tables_audited 同增 (#116+哨兵④+AUD-014a)


# --- DataLayer 接线 ---

def test_freshness_audit_pipeline_registered():
    from data_layer import DataLayer
    import inspect
    src = inspect.getsource(DataLayer._register_pipelines)
    assert "FreshnessAuditPipeline" in src

def test_get_freshness_audit_read_api():
    from data_layer import DataLayer
    dl = DataLayer.__new__(DataLayer)
    db = _FakeDB()
    db.conn.execute(
        "CREATE TABLE freshness_audit (date TEXT, table_name TEXT, "
        "expected_by TEXT, actual_at TEXT, delay_min INTEGER, status TEXT, "
        "created_at TEXT)")
    db.conn.execute(
        "INSERT INTO freshness_audit VALUES ('2026-08-18', 'daily_klines', "
        "'2026-08-18', '2026-08-18', 0, 'ok', '2026-08-18 16:00:00')")
    db.conn.commit()
    dl.db = db
    rows = dl.get_freshness_audit("2026-08-18")
    assert len(rows) == 1
    assert rows[0]["status"] == "ok"

def test_get_freshness_audit_missing_table():
    """表不存在 (管线未首跑) → 空列表不抛错"""
    from data_layer import DataLayer
    dl = DataLayer.__new__(DataLayer)
    dl.db = _FakeDB()
    assert dl.get_freshness_audit("2026-08-18") == []
