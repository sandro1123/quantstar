"""DL-21: stock_info.list_date 回填 — 回填脚本 + batch_upsert 保留语义

:memory:/tmp 库, 无生产写。验证:
- batch_upsert 不再抹掉已有 list_date/industry/delist_date (根因修复)
- 回填计划: 填空 / 覆盖占位 / 真实日期不误改
- dry-run 不写, apply+backup 写且备份
"""

import sqlite3

import pytest

from scripts.backfill_list_date import (
    backfill,
    detect_placeholder_dates,
    plan_backfill,
)


def _mk_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE stock_info (
            code TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            industry TEXT DEFAULT '',
            market TEXT DEFAULT 'SZ',
            list_date TEXT DEFAULT '',
            is_active INTEGER DEFAULT 1,
            delist_date TEXT DEFAULT ''
        )
    """)
    conn.commit()
    conn.close()
    return db_path


def _add(db_path, code, name="股", list_date="", industry=""):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO stock_info (code, name, industry, list_date) VALUES (?, ?, ?, ?)",
        (code, name, industry, list_date))
    conn.commit()
    conn.close()


def _get(db_path, code, col="list_date"):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        f"SELECT {col} FROM stock_info WHERE code = ?", (code,)).fetchone()
    conn.close()
    return row[col] if row else None


# ---------- batch_upsert 保留语义 (根因修复) ----------

def test_batch_upsert_preserves_existing_list_date(tmp_path):
    """每日同步 (仅 code+name) 不得抹掉已有 list_date"""
    from common.database import Database
    from data_layer.repos.stock import StockRepo
    db_path = _mk_db(tmp_path)
    _add(db_path, "000001", list_date="19910403")
    db = Database(db_path)
    StockRepo(db).batch_upsert([{"code": "000001", "name": "平安银行"}])
    assert _get(db_path, "000001") == "19910403"


def test_batch_upsert_preserves_industry_and_delist(tmp_path):
    from common.database import Database
    from data_layer.repos.stock import StockRepo
    db_path = _mk_db(tmp_path)
    _add(db_path, "000001", industry="银行", list_date="19910403")
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE stock_info SET delist_date='20260101' WHERE code='000001'")
    conn.commit()
    conn.close()
    db = Database(db_path)
    StockRepo(db).batch_upsert([{"code": "000001", "name": "平安银行"}])
    assert _get(db_path, "000001", "industry") == "银行"
    assert _get(db_path, "000001", "delist_date") == "20260101"


def test_batch_upsert_new_record_inserts(tmp_path):
    from common.database import Database
    from data_layer.repos.stock import StockRepo
    db_path = _mk_db(tmp_path)
    db = Database(db_path)
    StockRepo(db).batch_upsert([
        {"code": "600519", "name": "贵州茅台", "list_date": "20010827"}])
    assert _get(db_path, "600519") == "20010827"


def test_batch_upsert_enriches_empty_with_source_value(tmp_path):
    """源带 list_date 时可填空 (富化路径)"""
    from common.database import Database
    from data_layer.repos.stock import StockRepo
    db_path = _mk_db(tmp_path)
    _add(db_path, "000001")  # 空 list_date
    db = Database(db_path)
    StockRepo(db).batch_upsert([
        {"code": "000001", "name": "平安银行", "list_date": "19910403"}])
    assert _get(db_path, "000001") == "19910403"


# ---------- 占位日期检测 ----------

def test_detect_placeholder_dates(tmp_path):
    db_path = _mk_db(tmp_path)
    for i in range(12):
        _add(db_path, f"83{i:04d}", list_date="20260518")  # 12 行同日 → 占位
    _add(db_path, "600519", list_date="20010827")  # 单行 → 真实
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ph = detect_placeholder_dates(conn)
    conn.close()
    assert "20260518" in ph
    assert "20010827" not in ph


# ---------- 回填计划 ----------

def test_plan_fills_empty(tmp_path):
    db_path = _mk_db(tmp_path)
    _add(db_path, "000001")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    plan = plan_backfill(conn, {"000001": "19910403"})
    conn.close()
    assert len(plan) == 1
    assert plan[0]["reason"] == "fill_empty"
    assert plan[0]["new"] == "19910403"


def test_plan_overwrites_placeholder(tmp_path):
    db_path = _mk_db(tmp_path)
    for i in range(12):
        _add(db_path, f"83{i:04d}", list_date="20260518")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    plan = plan_backfill(conn, {"830000": "20150320"})
    conn.close()
    assert len(plan) == 1
    assert plan[0]["reason"] == "overwrite_placeholder"


def test_plan_never_touches_real_dates(tmp_path):
    """真实日期 (非占位) 即使与源不同也不误改"""
    db_path = _mk_db(tmp_path)
    _add(db_path, "600519", list_date="20010827")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    plan = plan_backfill(conn, {"600519": "19990101"})
    conn.close()
    assert len(plan) == 0


# ---------- dry-run / apply ----------

def test_dry_run_does_not_write(tmp_path):
    db_path = _mk_db(tmp_path)
    _add(db_path, "000001")
    stats = backfill(db_path, {"000001": "19910403"}, apply=False)
    assert stats["fill_empty"] == 1
    assert stats["applied"] == 0
    assert _get(db_path, "000001") == ""


def test_apply_writes_and_backs_up(tmp_path):
    db_path = _mk_db(tmp_path)
    _add(db_path, "000001")
    stats = backfill(db_path, {"000001": "19910403"}, apply=True, backup=True)
    assert stats["applied"] == 1
    assert _get(db_path, "000001") == "19910403"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    bak = conn.execute(
        "SELECT list_date FROM list_date_backup WHERE code='000001'").fetchone()
    conn.close()
    assert bak["list_date"] == ""  # 备份保留原值


def test_apply_skips_codes_without_source(tmp_path):
    db_path = _mk_db(tmp_path)
    _add(db_path, "000001")
    _add(db_path, "999999")  # 源无此码
    stats = backfill(db_path, {"000001": "19910403"}, apply=True)
    assert stats["applied"] == 1
    assert _get(db_path, "999999") == ""
