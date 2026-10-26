"""Tests for ops_layer/version_tracker.py"""
import pytest
from common.database import Database
from ops_layer.core.tables import OPS_TABLES_DDL
from ops_layer.version_tracker import VersionTracker


@pytest.fixture
def db():
    database = Database(":memory:")
    conn = database.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.commit()
    yield database
    database.close()


def test_record_version_returns_version_info(db):
    vt = VersionTracker(db)
    info = vt.record_version()
    assert info.code_version
    assert info.code_branch
    assert info.deployed_at


def test_record_version_persists(db):
    vt = VersionTracker(db)
    vt.record_version()
    conn = db.get_connection()
    row = conn.execute("SELECT * FROM version_history ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None
    assert row["code_version"] is not None
    assert row["deployed_at"] is not None
