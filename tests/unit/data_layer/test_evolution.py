"""Tests for data_layer/evolution — Schema 演进管理"""

import json
import pytest
from common.database import Database
from data_layer.evolution.schema import SchemaEvolution, NewField, ValidationResult, PromotionResult


@pytest.fixture
def db():
    database = Database(":memory:")
    yield database
    database.close()


@pytest.fixture
def db_with_table(db):
    """创建带测试表的数据库"""
    conn = db.get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_klines (
            code TEXT NOT NULL,
            date TEXT NOT NULL,
            close REAL DEFAULT 0,
            PRIMARY KEY (code, date)
        )
    """)
    conn.commit()
    return db


@pytest.fixture
def evolution(db_with_table):
    return SchemaEvolution(db_with_table)


# ── Step 1: detect_new_fields ──────────────────────────────────

def test_detect_new_fields(evolution):
    data = {"code": "000001", "date": "2026-01-01", "close": 10.0, "new_field": "value"}
    new_fields = evolution.detect_new_fields("daily_klines", data, "akshare")

    assert len(new_fields) == 1
    assert new_fields[0].field_name == "new_field"
    assert new_fields[0].field_type == "TEXT"
    assert new_fields[0].sample_value == "value"
    assert new_fields[0].source == "akshare"
    assert new_fields[0].table == "daily_klines"


def test_detect_new_fields_none(evolution):
    data = {"code": "000001", "date": "2026-01-01", "close": 10.0}
    new_fields = evolution.detect_new_fields("daily_klines", data, "akshare")
    assert len(new_fields) == 0


def test_detect_new_fields_multiple(evolution):
    data = {"code": "000001", "date": "2026-01-01", "field_a": 1, "field_b": 2.0, "field_c": "x"}
    new_fields = evolution.detect_new_fields("daily_klines", data, "test")
    names = {f.field_name for f in new_fields}
    assert names == {"field_a", "field_b", "field_c"}


# ── Step 2: quarantine ─────────────────────────────────────────

def test_quarantine(evolution):
    field = NewField(
        table="daily_klines",
        field_name="new_field",
        field_type="TEXT",
        sample_value="test_value",
        source="akshare"
    )
    evolution.quarantine(field)

    conn = evolution.db.get_connection()
    rows = conn.execute(
        "SELECT * FROM schema_quarantine WHERE field_name = 'new_field'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["table_name"] == "daily_klines"
    assert rows[0]["field_type"] == "TEXT"
    assert rows[0]["source"] == "akshare"
    assert rows[0]["status"] == "pending"
    assert json.loads(rows[0]["sample_value"]) == "test_value"


# ── Step 3: validate ───────────────────────────────────────────

def test_validate_valid(evolution):
    """样本类型一致、null率低 → 通过"""
    conn = evolution.db.get_connection()
    for v in ["a", "b", "c"]:
        conn.execute(
            "INSERT INTO schema_quarantine (table_name, field_name, field_type, sample_value, source) "
            "VALUES (?, ?, ?, ?, ?)",
            ("daily_klines", "new_field", "TEXT", json.dumps(v), "test")
        )
    conn.commit()

    result = evolution.validate("daily_klines", "new_field")
    assert result.valid is True
    assert result.samples == 3


def test_validate_no_samples(evolution):
    result = evolution.validate("daily_klines", "nonexistent")
    assert result.valid is False
    assert "No samples" in result.reason


def test_validate_high_null_rate(evolution):
    """超过 50% null → 拒绝"""
    conn = evolution.db.get_connection()
    for v in [None, None, None, "ok", "ok"]:
        conn.execute(
            "INSERT INTO schema_quarantine (table_name, field_name, field_type, sample_value, source) "
            "VALUES (?, ?, ?, ?, ?)",
            ("daily_klines", "f", "TEXT", json.dumps(v), "test")
        )
    conn.commit()

    result = evolution.validate("daily_klines", "f")
    assert result.valid is False
    assert "Null rate" in result.reason


def test_validate_inconsistent_types(evolution):
    """混合类型 → 拒绝"""
    conn = evolution.db.get_connection()
    for v in ["str", 123]:
        conn.execute(
            "INSERT INTO schema_quarantine (table_name, field_name, field_type, sample_value, source) "
            "VALUES (?, ?, ?, ?, ?)",
            ("daily_klines", "f", "TEXT", json.dumps(v), "test")
        )
    conn.commit()

    result = evolution.validate("daily_klines", "f")
    assert result.valid is False
    assert "Inconsistent types" in result.reason


# ── Step 4: promote ────────────────────────────────────────────

def test_promote_success(evolution):
    conn = evolution.db.get_connection()
    conn.execute(
        "INSERT INTO schema_quarantine (table_name, field_name, field_type, sample_value, source) "
        "VALUES (?, ?, ?, ?, ?)",
        ("daily_klines", "new_col", "TEXT", json.dumps("v"), "test")
    )
    conn.commit()

    result = evolution.promote("daily_klines", "new_col", "TEXT")
    assert result.success is True

    columns = evolution._get_table_columns("daily_klines")
    assert "new_col" in columns

    row = conn.execute(
        "SELECT status FROM schema_quarantine WHERE field_name = 'new_col'"
    ).fetchone()
    assert row["status"] == "promoted"


def test_promote_nonexistent_table(evolution):
    result = evolution.promote("no_such_table", "f", "TEXT")
    assert result.success is False


# ── Step 5: _infer_type ────────────────────────────────────────

def test_infer_type():
    se = SchemaEvolution.__new__(SchemaEvolution)
    assert se._infer_type(123) == "INTEGER"
    assert se._infer_type(3.14) == "REAL"
    assert se._infer_type("text") == "TEXT"
    assert se._infer_type(None) == "TEXT"
    assert se._infer_type(True) == "INTEGER"
    assert se._infer_type([1, 2]) == "TEXT"


# ── Step 6: _get_table_columns ─────────────────────────────────

def test_get_table_columns(evolution):
    cols = evolution._get_table_columns("daily_klines")
    assert cols == {"code", "date", "close"}


def test_get_table_columns_nonexistent(evolution):
    cols = evolution._get_table_columns("no_such_table")
    assert cols == set()


# ── Integration: full lifecycle ────────────────────────────────

def test_full_lifecycle(evolution):
    # detect
    data = {"code": "000001", "date": "2026-01-01", "close": 10.0, "extra": 99}
    new_fields = evolution.detect_new_fields("daily_klines", data, "akshare")
    assert len(new_fields) == 1

    # quarantine
    evolution.quarantine(new_fields[0])

    # validate
    result = evolution.validate("daily_klines", "extra")
    assert result.valid is True

    # promote
    promote_result = evolution.promote("daily_klines", "extra", "INTEGER")
    assert promote_result.success is True

    # verify
    columns = evolution._get_table_columns("daily_klines")
    assert "extra" in columns


# ── Security: SQL injection prevention ────────────────────────

def test_promote_rejects_sql_injection_table(evolution):
    result = evolution.promote("daily_klines; DROP TABLE users--", "col", "TEXT")
    assert result.success is False
    assert result.error is not None
    assert "Invalid table name" in result.error.suggestion


def test_promote_rejects_sql_injection_field(evolution):
    result = evolution.promote("daily_klines", "col; DROP TABLE users--", "TEXT")
    assert result.success is False
    assert result.error is not None
    assert "Invalid field name" in result.error.suggestion


def test_promote_rejects_invalid_field_type(evolution):
    result = evolution.promote("daily_klines", "col", "VARCHAR(255)")
    assert result.success is False
    assert result.error is not None
    assert "Invalid field type" in result.error.suggestion


def test_get_table_columns_rejects_injection(evolution):
    cols = evolution._get_table_columns("daily_klines; DROP TABLE users--")
    assert cols == set()


# ── Return types ─────────────────────────────────────────────

def test_validate_returns_dataclass(evolution):
    result = evolution.validate("daily_klines", "nonexistent")
    assert isinstance(result, ValidationResult)


def test_promote_returns_dataclass(evolution):
    result = evolution.promote("daily_klines", "col", "TEXT")
    assert isinstance(result, PromotionResult)
