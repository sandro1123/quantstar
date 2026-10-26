"""DL-13: pipeline_runs 噪音清理脚本 — dry-run/apply/备份

:memory: 库, 无生产写。
"""

import sqlite3

from ops_layer.core.tables import OPS_TABLES_DDL
from scripts.cleanup_pipeline_runs_noise import (
    BACKUP_TABLE,
    cleanup,
    count_noise,
)


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(OPS_TABLES_DDL)
    return conn


def _seed(conn, noise=5, real=3):
    for i in range(noise):
        conn.execute(
            "INSERT INTO pipeline_runs (run_id, pipeline_name, date, start_time, status) "
            "VALUES (?, 'api_health_check', '2026-08-18', '2026-08-18 10:00:00', 'success')",
            (f"noise_{i}",),
        )
    for i in range(real):
        conn.execute(
            "INSERT INTO pipeline_runs (run_id, pipeline_name, date, start_time, status) "
            "VALUES (?, 'daily_data_pipeline', '2026-08-18', '2026-08-18 09:00:00', 'success')",
            (f"real_{i}",),
        )
    conn.commit()


def test_count_noise():
    conn = _db()
    _seed(conn)
    assert count_noise(conn) == {"api_health_check": 5}


def test_dry_run_does_not_delete():
    conn = _db()
    _seed(conn)
    stats = cleanup(conn, apply=False)
    assert stats["deleted"] == 0
    assert stats["applied"] is False
    n = conn.execute("SELECT COUNT(*) FROM pipeline_runs").fetchone()[0]
    assert n == 8  # 全在


def test_apply_deletes_noise_keeps_real():
    conn = _db()
    _seed(conn)
    stats = cleanup(conn, apply=True)
    assert stats["deleted"] == 5
    rows = conn.execute(
        "SELECT DISTINCT pipeline_name FROM pipeline_runs").fetchall()
    assert [r["pipeline_name"] for r in rows] == ["daily_data_pipeline"]


def test_apply_with_backup_creates_backup_table():
    conn = _db()
    _seed(conn)
    cleanup(conn, apply=True, backup=True)
    n = conn.execute(f"SELECT COUNT(*) FROM {BACKUP_TABLE}").fetchone()[0]
    assert n == 5


def test_no_noise_is_noop():
    conn = _db()
    _seed(conn, noise=0)
    stats = cleanup(conn, apply=True)
    assert stats["deleted"] == 0
    assert stats["found"] == {}
