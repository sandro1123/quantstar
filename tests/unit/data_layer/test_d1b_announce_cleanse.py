"""D1-b 清创 — TDD 测试 (红先行)。

病面定谳 (probe 09-12): dup 5358 (双键共存删病键) + orphan 5381 (平移+1);
对拍 5357/5358 announce 差恰 +1, 特例 1 行 (002762 公告修正二次写, 以标准行为准)。
"""
import sqlite3

import pytest

from scripts.backfill_announce_date import ms_to_date
from scripts.backfill_announce_date import BJT  # D1-b: 修法①同款 BJT 常量
from scripts.cleanse_announce_offbyone import classify, run_cleanse, _shift

FULL_DDL = """
CREATE TABLE IF NOT EXISTS stock_financials (
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    eps REAL DEFAULT 0,
    roe REAL DEFAULT 0,
    revenue REAL DEFAULT 0,
    profit REAL DEFAULT 0,
    total_assets REAL DEFAULT 0,
    total_liabilities REAL DEFAULT 0,
    net_assets REAL DEFAULT 0,
    operating_cash_flow REAL DEFAULT 0,
    gross_margin REAL DEFAULT 0,
    net_margin REAL DEFAULT 0,
    announce_date TEXT DEFAULT '',
    caliber TEXT DEFAULT 'cumulative',
    PRIMARY KEY (code, date)
)
"""


def _db(rows):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(FULL_DDL)
    for r in rows:
        conn.execute(
            "INSERT INTO stock_financials (code, date, eps, announce_date) VALUES (?, ?, ?, ?)",
            (r["code"], r["date"], r.get("eps", 1.0), r.get("announce_date", "")))
    conn.commit()
    return conn


class TestMsToDateBJT:
    def test_bjt_midnight_reads_correct_day(self):
        # 1786636800000 = 2026-08-14 00:00 BJT = 08-13 16:00 UTC
        assert ms_to_date(1786636800000) == "2026-08-14"

    def test_period_ms_reads_quarter_end(self):
        # 1782748800000 = 2026-06-30 00:00 BJT (季末零点, 修法①判例)
        assert ms_to_date(1782748800000) == "2026-06-30"

    def test_bjt_constant_is_utc8(self):
        from datetime import timedelta
        assert BJT.utcoffset(None) == timedelta(hours=8)


class TestClassify:
    def test_dup_and_orphan_sorted(self):
        conn = _db([
            {"code": "600001", "date": "2026-06-29", "announce_date": "2026-08-25"},
            {"code": "600001", "date": "2026-06-30", "announce_date": "2026-08-26"},  # dup 对
            {"code": "600002", "date": "2026-03-30", "announce_date": "2026-04-29"},  # orphan
            {"code": "600003", "date": "2026-03-31", "announce_date": "2026-04-30"},  # 健康
        ])
        c = classify(conn)
        assert c["dup"] == [("600001", "2026-06-29")]
        assert [x[0] for x in c["orphan"]] == ["600002"]

    def test_standard_keys_not_sick(self):
        conn = _db([{"code": "600003", "date": "2026-03-31"}])
        c = classify(conn)
        assert c["dup"] == [] and c["orphan"] == []


class TestCleanse:
    def test_dry_no_write(self):
        conn = _db([
            {"code": "600001", "date": "2026-06-29", "announce_date": "2026-08-25"},
            {"code": "600001", "date": "2026-06-30", "announce_date": "2026-08-26"},
        ])
        run_cleanse(conn, dry_run=True)
        n = conn.execute("SELECT COUNT(*) FROM stock_financials").fetchone()[0]
        assert n == 2  # dry 零写

    def test_apply_deletes_dup_sick_row(self):
        conn = _db([
            {"code": "600001", "date": "2026-06-29", "announce_date": "2026-08-25"},
            {"code": "600001", "date": "2026-06-30", "announce_date": "2026-08-26"},
        ])
        r = run_cleanse(conn, dry_run=False)
        assert r["n_delete_dup"] == 1
        rows = conn.execute(
            "SELECT date, announce_date FROM stock_financials").fetchall()
        assert len(rows) == 1
        assert rows[0]["date"] == "2026-06-30"
        assert rows[0]["announce_date"] == "2026-08-26"  # 标准键真值保留

    def test_apply_shifts_orphan(self):
        conn = _db([{"code": "600002", "date": "2026-03-30", "announce_date": "2026-04-29"}])
        r = run_cleanse(conn, dry_run=False)
        assert r["n_shift_orphan"] == 1
        row = conn.execute(
            "SELECT date, announce_date, eps FROM stock_financials").fetchone()
        assert row["date"] == "2026-03-31"
        assert row["announce_date"] == "2026-04-30"  # announce +1
        assert row["eps"] == 1.0  # 值拷贝不丢

    def test_apply_idempotent_rerun(self):
        rows = [
            {"code": "600001", "date": "2026-06-29", "announce_date": "2026-08-25"},
            {"code": "600001", "date": "2026-06-30", "announce_date": "2026-08-26"},
            {"code": "600002", "date": "2026-03-30", "announce_date": "2026-04-29"},
        ]
        conn = _db(rows)
        run_cleanse(conn, dry_run=False)
        r2 = run_cleanse(conn, dry_run=False)  # 二跑
        assert r2["n_delete_dup"] == 0
        assert r2["n_shift_orphan"] == 0
        n = conn.execute("SELECT COUNT(*) FROM stock_financials").fetchone()[0]
        assert n == 2  # dup 删一留一 + orphan 平移一行

    def test_outlier_pair_standard_wins(self):
        """特例 (002762 型): dup 对 announce 差 9 非同源 — 标准键行为准"""
        conn = _db([
            {"code": "002762", "date": "2026-06-29", "announce_date": "2026-08-24"},
            {"code": "002762", "date": "2026-06-30", "announce_date": "2026-09-02"},
        ])
        run_cleanse(conn, dry_run=False)
        row = conn.execute("SELECT announce_date FROM stock_financials").fetchone()
        assert row["announce_date"] == "2026-09-02"  # 标准键 (修法①后真值) 保留

    def test_empty_announce_orphan_shifts_period_only(self):
        """orphan 无 announce 值: period 平移, announce 留空 (不造假)"""
        conn = _db([{"code": "600004", "date": "2026-06-29", "announce_date": ""}])
        run_cleanse(conn, dry_run=False)
        row = conn.execute("SELECT date, announce_date FROM stock_financials").fetchone()
        assert row["date"] == "2026-06-30"
        assert row["announce_date"] == ""
