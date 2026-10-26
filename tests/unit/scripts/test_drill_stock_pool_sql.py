"""Tests for B3-R1 drill_stock_pool_sql.py."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent / "scripts"))
from drill_stock_pool_sql import (
    fingerprint_db,
    logical_fingerprint,
    physical_fingerprint,
    sha256_file,
    sidecar_state,
    table_logical_hash,
    table_row_count,
    table_schema,
)


@pytest.fixture
def drill_db(tmp_path):
    path = tmp_path / "test.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE stock_info (code TEXT, name TEXT, market TEXT, is_active INTEGER)")
    conn.execute("INSERT INTO stock_info VALUES ('000001', 'test1', 'SZ', 1)")
    conn.execute("INSERT INTO stock_info VALUES ('000013', 'test2', 'SZ', 1)")
    conn.execute("INSERT INTO stock_info VALUES ('920808', 'test3', 'SZ', 1)")
    conn.commit()
    conn.close()
    return path


def test_sha256_file(drill_db):
    h = sha256_file(str(drill_db))
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_table_row_count(drill_db):
    conn = sqlite3.connect(str(drill_db))
    assert table_row_count(conn, "stock_info") == 3
    conn.close()


def test_table_schema(drill_db):
    conn = sqlite3.connect(str(drill_db))
    schema = table_schema(conn, "stock_info")
    cols = {s["name"]: s for s in schema}
    assert "code" in cols
    assert "is_active" in cols
    assert "market" in cols
    conn.close()


def test_table_logical_hash(drill_db):
    conn = sqlite3.connect(str(drill_db))
    h1 = table_logical_hash(conn, "stock_info")
    assert len(h1) == 64
    # Same data should produce same hash
    h2 = table_logical_hash(conn, "stock_info")
    assert h1 == h2
    conn.close()


def test_fingerprint_db(drill_db):
    conn = sqlite3.connect(str(drill_db))
    fp = fingerprint_db(conn, str(drill_db))
    assert "sha256" in fp
    assert "size_bytes" in fp
    assert "tables" in fp
    assert "stock_info" in fp["tables"]
    assert fp["tables"]["stock_info"]["row_count"] == 3
    conn.close()


def test_logical_hash_change_after_update(drill_db):
    conn = sqlite3.connect(str(drill_db))
    h_before = table_logical_hash(conn, "stock_info")
    conn.execute("UPDATE stock_info SET is_active = 0 WHERE code = '000013'")
    conn.commit()
    h_after = table_logical_hash(conn, "stock_info")
    assert h_before != h_after
    conn.execute("UPDATE stock_info SET is_active = 1 WHERE code = '000013'")
    conn.commit()
    h_restored = table_logical_hash(conn, "stock_info")
    assert h_restored == h_before
    conn.close()


def test_physical_fingerprint_at_rest(drill_db):
    """physical_fingerprint records sidecars + honest confidence after close."""
    fp = physical_fingerprint(str(drill_db))
    assert "sha256" in fp
    assert "size_bytes" in fp
    assert "sidecars" in fp
    assert fp["sidecars"]["wal_exists"] is False
    assert fp["physical_sha_confidence"] == "ok"


def test_physical_sha_changes_after_commit_close(tmp_path):
    """Nova B3 fix: after commit + close (no wal), physical SHA reflects state."""
    path = tmp_path / "p.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()
    before = physical_fingerprint(str(path))["sha256"]

    conn = sqlite3.connect(str(path))
    conn.execute("UPDATE t SET x = 2")
    conn.commit()
    conn.close()
    after = physical_fingerprint(str(path))["sha256"]

    assert before != after
    assert sidecar_state(str(path))["wal_exists"] is False


def test_fingerprint_db_marks_confidence(drill_db):
    """Combined fingerprint carries a physical-sha confidence field."""
    conn = sqlite3.connect(str(drill_db))
    fp = fingerprint_db(conn, str(drill_db))
    assert fp["physical_sha_confidence"].startswith("ok")
    assert "logical_hash" in fp["tables"]["stock_info"]
    conn.close()


def test_logical_fingerprint_roundtrip(drill_db):
    """Logical fingerprint is deterministic; physical hash may differ."""
    conn = sqlite3.connect(str(drill_db))
    lf = logical_fingerprint(conn, str(drill_db))
    assert lf["tables"]["stock_info"]["row_count"] == 3
    conn.close()

    # change + close, then re-fingerprint logically
    conn = sqlite3.connect(str(drill_db))
    conn.execute("UPDATE stock_info SET is_active = 0 WHERE code = '000013'")
    conn.commit()
    conn.close()
    conn = sqlite3.connect(str(drill_db))
    lf2 = logical_fingerprint(conn, str(drill_db))
    conn.close()
    assert lf2["tables"]["stock_info"]["logical_hash"] != lf["tables"]["stock_info"]["logical_hash"]