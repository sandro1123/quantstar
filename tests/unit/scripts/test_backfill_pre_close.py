"""DL-14 (Part 1): pre_close=0 回填 — dry-run 默认, apply 才写 (写库三步曲)

:memory: 库, 无生产写。验证回填逻辑取当日之前最近有效 close。
"""

import sqlite3

from scripts.backfill_pre_close import backfill, find_backfillable

DATE = "2026-07-29"


class _FakeDB:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

    def get_connection(self):
        return self.conn


def _mk_db():
    db = _FakeDB()
    db.conn.execute(
        "CREATE TABLE daily_klines (code TEXT, date TEXT, close REAL, "
        "pre_close REAL, PRIMARY KEY (code, date))")
    db.conn.commit()
    return db


def _add(db, code, date, close, pre_close):
    db.conn.execute(
        "INSERT INTO daily_klines VALUES (?, ?, ?, ?)",
        (code, date, close, pre_close))
    db.conn.commit()


def test_find_backfillable_picks_prior_close():
    db = _mk_db()
    _add(db, "000001", "2026-07-28", 10.0, 9.5)   # 前一日 close=10
    _add(db, "000001", DATE, 11.0, 0.0)            # 当日 pre_close=0 待回填
    rows = find_backfillable(db)
    assert len(rows) == 1
    assert rows[0]["code"] == "000001"
    assert rows[0]["new_pre_close"] == 10.0  # 取前一日 close

def test_find_backfillable_skips_zero_close_prior():
    """前序 close=0 (无效) 不作为回填源"""
    db = _mk_db()
    _add(db, "000001", "2026-07-28", 0.0, 0.0)    # 前一日 close=0 无效
    _add(db, "000001", "2026-07-27", 8.0, 7.5)    # 更前一日 close=8 有效
    _add(db, "000001", DATE, 11.0, 0.0)
    rows = find_backfillable(db)
    assert rows[0]["new_pre_close"] == 8.0  # 跳过 0, 取 8

def test_find_backfillable_no_prior_returns_none():
    db = _mk_db()
    _add(db, "000001", DATE, 11.0, 0.0)  # 无任何前序
    rows = find_backfillable(db)
    assert len(rows) == 1
    assert rows[0]["new_pre_close"] is None

def test_dry_run_does_not_write():
    db = _mk_db()
    _add(db, "000001", "2026-07-28", 10.0, 9.5)
    _add(db, "000001", DATE, 11.0, 0.0)
    stats = backfill(db, apply=False)
    assert stats["total_zero"] == 1
    assert stats["backfillable"] == 1
    assert stats["applied"] == 0
    # 未写: pre_close 仍为 0
    row = db.conn.execute(
        "SELECT pre_close FROM daily_klines WHERE date = ?", (DATE,)).fetchone()
    assert row["pre_close"] == 0.0

def test_apply_writes_and_backup():
    db = _mk_db()
    _add(db, "000001", "2026-07-28", 10.0, 9.5)
    _add(db, "000001", DATE, 11.0, 0.0)
    stats = backfill(db, apply=True, backup=True)
    assert stats["applied"] == 1
    row = db.conn.execute(
        "SELECT pre_close FROM daily_klines WHERE date = ?", (DATE,)).fetchone()
    assert row["pre_close"] == 10.0
    # 备份表存在且含原值
    bak = db.conn.execute(
        "SELECT pre_close FROM pre_close_backup").fetchone()
    assert bak["pre_close"] == 0.0

def test_apply_skips_no_prior():
    db = _mk_db()
    _add(db, "000001", DATE, 11.0, 0.0)  # 无前序
    stats = backfill(db, apply=True)
    assert stats["backfillable"] == 0
    assert stats["skipped_no_prior"] == 1
    assert stats["applied"] == 0

def test_only_zero_pre_close_touched():
    """非 0 的 pre_close 不被改动"""
    db = _mk_db()
    _add(db, "000001", "2026-07-28", 10.0, 9.5)
    _add(db, "000001", DATE, 11.0, 10.0)  # pre_close 正常
    stats = backfill(db, apply=True)
    assert stats["total_zero"] == 0
    assert stats["applied"] == 0
