"""ValuationRepo NULL 语义测试（仅使用临时内存库，不触碰正式库）。"""

import sqlite3

import pytest

from common.database import Database
from data_layer.repos.valuation import ValuationRepo


def make_repo():
    db = Database(":memory:")
    return db, ValuationRepo(db)


def test_new_schema_allows_null_valuation_columns():
    db, repo = make_repo()
    repo._ensure_table()
    conn = db.get_connection()
    info = {
        row["name"]: row
        for row in conn.execute("PRAGMA table_info(stock_valuation)").fetchall()
    }
    for field in (
        "pe_ttm",
        "pb_mrq",
        "ps_ttm",
        "market_cap",
        "total_shares",
        "dividend_yield",
    ):
        assert info[field]["notnull"] == 0, f"{field} must be nullable"
        assert info[field]["dflt_value"] is None, f"{field} must not DEFAULT 0"


def test_explicit_none_write_roundtrips_as_null():
    db, repo = make_repo()
    repo.batch_upsert(
        [
            {
                "code": "600000",
                "date": "2026-07-28",
                "pe_ttm": 6.0,
                "pb_mrq": None,
                "ps_ttm": None,
                "market_cap": None,
                "total_shares": None,
                "dividend_yield": None,
            }
        ]
    )
    rows = repo.get_range("600000", "2026-07-28", "2026-07-28")
    assert len(rows) == 1
    row = rows[0]
    assert row["pe_ttm"] == 6.0
    assert row["pb_mrq"] is None
    assert row["ps_ttm"] is None
    assert row["market_cap"] is None
    assert row["total_shares"] is None
    assert row["dividend_yield"] is None


def test_missing_optional_fields_do_not_become_zero():
    db, repo = make_repo()
    repo.batch_upsert([{"code": "600000", "date": "2026-07-28", "pe_ttm": 6.0}])
    row = repo.get_range("600000", "2026-07-28", "2026-07-28")[0]
    assert row["pe_ttm"] == 6.0
    assert row["pb_mrq"] is None
    assert row["ps_ttm"] is None
    assert row["market_cap"] is None
    assert row["total_shares"] is None
    assert row["dividend_yield"] is None


def test_code_date_not_null_and_primary_key():
    db, repo = make_repo()
    repo._ensure_table()
    conn = db.get_connection()
    info = {
        row["name"]: row
        for row in conn.execute("PRAGMA table_info(stock_valuation)").fetchall()
    }
    assert info["code"]["notnull"] == 1
    assert info["date"]["notnull"] == 1
    assert info["code"]["pk"] != 0
    assert info["date"]["pk"] != 0


def test_missing_code_or_date_raises_integrity_error():
    db, repo = make_repo()
    with pytest.raises(sqlite3.IntegrityError):
        repo.batch_upsert([{"code": None, "date": "2026-07-28", "pe_ttm": 6.0}])
    with pytest.raises(sqlite3.IntegrityError):
        repo.batch_upsert([{"code": "600000", "date": None, "pe_ttm": 6.0}])


def test_duplicate_code_date_upsert_replaces_not_duplicates():
    db, repo = make_repo()
    repo.batch_upsert([{"code": "600000", "date": "2026-07-28", "pe_ttm": 6.0}])
    repo.batch_upsert([{"code": "600000", "date": "2026-07-28", "pe_ttm": 7.5}])
    rows = repo.get_range("600000", "2026-07-28", "2026-07-28")
    assert len(rows) == 1
    assert rows[0]["pe_ttm"] == 7.5
