"""DL-11: 死代码处置 — 5 子系统逐一接线或删除

DataQuality → quality_check 管线接线; SourceHealthChecker/DataIntegrityChecker →
DataLayer 读 API 接线; SchemaEvolution → 标注未启用; Checkpoint → 删除 (有等价机制)。
mock + :memory: 库, 无网络无生产库。
"""

import sqlite3

from data_layer.pipelines.base import PipelineInput
from data_layer.pipelines.quality_check import (
    QualityCheckPipeline, _classify_level, _extract_table,
)


class _FakeDB:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

    def get_connection(self):
        return self.conn


def _mk_klines_db(date="2026-08-18", rows=0):
    """建 daily_klines + stock_info, 可选插入 rows 行当日数据。"""
    db = _FakeDB()
    db.conn.execute(
        "CREATE TABLE daily_klines (code TEXT, date TEXT, open REAL, high REAL, "
        "low REAL, close REAL, volume REAL, amount REAL, pre_close REAL, "
        "change_pct REAL, PRIMARY KEY (code, date))")
    db.conn.execute(
        "CREATE TABLE stock_info (code TEXT PRIMARY KEY, name TEXT, "
        "is_active INTEGER DEFAULT 1)")
    # DataQuality.DATE_TABLES 其余 4 表须存在, 否则 "表不存在" flag 误报
    for t in ("factor_daily", "stock_financials", "stock_valuation",
              "stock_moneyflow"):
        db.conn.execute(
            f"CREATE TABLE {t} (code TEXT, date TEXT, PRIMARY KEY (code, date))")
    for i in range(rows):
        code = f"{i:06d}"
        db.conn.execute(
            "INSERT INTO stock_info VALUES (?, ?, 1)", (code, f"股{i}"))
        db.conn.execute(
            "INSERT INTO daily_klines VALUES (?, ?, 10, 11, 9, 10.5, 1000, 10000, "
            "10, 5.0)", (code, date))
        # 估值行须与行情同覆盖, 否则 stock_valuation 覆盖率 flag 误报
        db.conn.execute(
            "INSERT INTO stock_valuation VALUES (?, ?)", (code, date))
    db.conn.commit()
    return db


# --- _classify_level / _extract_table ---

def test_classify_level_critical():
    assert _classify_level("daily_klines: 2026-08-18 无数据") == "CRITICAL"
    assert _classify_level("factor_daily: 表不存在") == "CRITICAL"

def test_classify_level_error():
    assert _classify_level("daily_klines: 覆盖率 50%，低于 85%") == "ERROR"
    assert _classify_level("目标日期格式非法") == "ERROR"

def test_extract_table():
    assert _extract_table("daily_klines: 无数据") == "daily_klines"
    assert _extract_table("无表名前缀") == ""


# --- QualityCheckPipeline ---

def test_quality_check_creates_table():
    db = _mk_klines_db(rows=10)
    pipe = QualityCheckPipeline(db=db)
    pipe.run(PipelineInput(date="2026-08-18"))
    tables = {r[0] for r in db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "quality_checks" in tables

def test_quality_check_writes_flags():
    """无数据 → CRITICAL flag 落表"""
    db = _mk_klines_db(rows=0)
    pipe = QualityCheckPipeline(db=db)
    result = pipe.run(PipelineInput(date="2026-08-18"))
    assert result.status in ("success", "partial")
    rows = db.conn.execute(
        "SELECT level, detail FROM quality_checks WHERE date='2026-08-18'"
    ).fetchall()
    assert len(rows) >= 1
    levels = {r["level"] for r in rows}
    assert "CRITICAL" in levels  # daily_klines 无数据

def test_quality_check_pass_row_when_clean():
    """全部通过 → 写一行 all_passed (保证表每日有记录)"""
    db = _mk_klines_db(rows=10)
    pipe = QualityCheckPipeline(db=db)
    result = pipe.run(PipelineInput(date="2026-08-18"))
    rows = db.conn.execute(
        "SELECT check_name, result FROM quality_checks WHERE date='2026-08-18'"
    ).fetchall()
    # 覆盖率 10/10=100% 通过, 应无 CRITICAL/ERROR flag
    assert result.metadata["is_passed"] is True
    assert any(r["check_name"] == "all_passed" for r in rows)

def test_quality_check_idempotent_rerun():
    """重跑不重复 (INSERT OR REPLACE)"""
    db = _mk_klines_db(rows=0)
    pipe = QualityCheckPipeline(db=db)
    pipe.run(PipelineInput(date="2026-08-18"))
    n1 = db.conn.execute("SELECT COUNT(*) FROM quality_checks").fetchone()[0]
    pipe.run(PipelineInput(date="2026-08-18"))
    n2 = db.conn.execute("SELECT COUNT(*) FROM quality_checks").fetchone()[0]
    assert n1 == n2

def test_quality_check_metadata_counts():
    db = _mk_klines_db(rows=0)
    pipe = QualityCheckPipeline(db=db)
    result = pipe.run(PipelineInput(date="2026-08-18"))
    assert result.metadata["flags"] >= 1
    assert result.metadata["critical"] >= 1


# --- DataLayer 接线: quality_check 管线注册 + 读 API ---

def test_quality_check_pipeline_registered():
    from data_layer import DataLayer
    dl = DataLayer.__new__(DataLayer)
    # 验证 _register_pipelines 会注册 quality_check
    import inspect
    src = inspect.getsource(DataLayer._register_pipelines)
    assert "QualityCheckPipeline" in src

def test_get_quality_checks_read_api():
    from data_layer import DataLayer
    dl = DataLayer.__new__(DataLayer)
    db = _FakeDB()
    db.conn.execute(
        "CREATE TABLE quality_checks (date TEXT, table_name TEXT, check_name TEXT, "
        "level TEXT, result TEXT, detail TEXT, created_at TEXT)")
    db.conn.execute(
        "INSERT INTO quality_checks VALUES ('2026-08-18', 'daily_klines', "
        "'flag_daily_klines', 'CRITICAL', 'fail', '无数据', '2026-08-18 16:00:00')")
    db.conn.commit()
    dl.db = db
    rows = dl.get_quality_checks("2026-08-18")
    assert len(rows) == 1
    assert rows[0]["check_name"] == "flag_daily_klines"

def test_get_source_health_read_api():
    from data_layer import DataLayer

    class _FakeSource:
        name = "fake"
        capabilities = {"kline"}
        def is_available(self):
            return True

    class _FakeRegistry:
        def get_all_sources(self):
            return [_FakeSource()]

    dl = DataLayer.__new__(DataLayer)
    dl._source_registry = _FakeRegistry()
    health = dl.get_source_health()
    assert "fake" in health
    assert health["fake"]["status"] == "healthy"

def test_get_integrity_summary_read_api():
    from data_layer import DataLayer
    dl = DataLayer.__new__(DataLayer)
    db = _mk_klines_db(rows=5)
    dl.db = db
    summary = dl.get_integrity_summary()
    assert summary["total_tables"] >= 2
    assert "daily_klines" in summary.get("tables", {}) or summary["total_tables"] > 0


# --- Checkpoint 删除 (有等价机制: orchestrator MAX(date) + 管线内增量) ---

def test_checkpoint_file_deleted():
    """DL-11: checkpoint.py 已删除 (等价机制存在, 不允许死代码)"""
    import os
    import data_layer
    base = os.path.dirname(os.path.abspath(data_layer.__file__))
    assert not os.path.exists(os.path.join(base, "checkpoint.py"))


# --- SchemaEvolution 标注未启用 ---

def test_schema_evolution_marked_not_enabled():
    """DL-11: data-factory-architecture.md 标注 SchemaEvolution 未启用"""
    import os
    import data_layer
    base = os.path.dirname(os.path.abspath(data_layer.__file__))
    doc = os.path.join(base, "..", "docs", "data-factory-architecture.md")
    doc = os.path.abspath(doc)
    if os.path.exists(doc):
        with open(doc, encoding="utf-8") as f:
            content = f.read()
        assert "未启用" in content or "not enabled" in content.lower()
