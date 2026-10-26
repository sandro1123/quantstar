"""W1: persistent duplicate observations, legacy migration and disk recovery.

These tests catch stale detail/last_seen, skipped legacy ALTERs, and healthy
samples leaving monitor alerts open. Only external notification/disk/time
boundaries are replaced; all alert state transitions use SQLite and real code.
"""
from types import SimpleNamespace

import pytest

from common.database import Database
from execution_layer.migrations import TARGET_VERSION, ensure_schema
from ops_layer import OpsLayer
from ops_layer.alert_manager import AlertManager
from ops_layer.core.models import Alert
from ops_layer.core.tables import OPS_TABLES_DDL
from ops_layer.orchestrator import PipelineOrchestrator
from ops_layer.pipeline_logger import PipelineLogger

NOW = "2026-09-17 14:00:00"
OLD = "2026-08-31 14:00:00"


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(AlertManager, "push_feishu", lambda *a, **kw: False)
    monkeypatch.setattr("ops_layer.alert_manager.bj_now", lambda: NOW)
    database = Database(":memory:")
    database.get_connection().executescript(OPS_TABLES_DDL)
    yield database
    database.close()


def seed(db, title="test event", status="open", layer="ops"):
    conn = db.get_connection()
    cur = conn.execute(
        "INSERT INTO alerts (severity, layer, title, detail, status, created_at, action) "
        "VALUES ('critical', ?, ?, 'old observation', ?, ?, 'escalated')",
        (layer, title, status, OLD),
    )
    conn.commit()
    return cur.lastrowid


def test_duplicate_refreshes_existing_row_and_preserves_event_identity(db):
    event_id = seed(db)
    closed_id = seed(db, status="resolved")
    manager = AlertManager(db)  # fresh process, no recent cache
    assert manager.send(Alert(severity="critical", layer="ops", title="test event",
                              detail="latest observation")) is False
    rows = db.get_connection().execute("SELECT * FROM alerts ORDER BY id").fetchall()
    assert len(rows) == 2
    row = dict(rows[0])
    assert row["id"] == event_id
    assert row["detail"] == "latest observation"
    assert row.get("last_seen") == NOW
    assert row["created_at"] == OLD
    assert row["action"] == "escalated"
    assert row["status"] == "open"
    assert rows[1]["id"] == closed_id
    assert rows[1]["detail"] == "old observation"


def test_first_observation_records_last_seen_and_recurrence_is_new_event(db, monkeypatch):
    pushes = []
    monkeypatch.setattr(AlertManager, "push_feishu", lambda *a, **kw: pushes.append(a) or False)
    am = AlertManager(db)
    alert = Alert(severity="critical", layer="ops", title="test event", detail="new")
    assert am.send(alert) is True  # admission, NOT delivery success
    row = dict(db.get_connection().execute("SELECT * FROM alerts").fetchone())
    assert row.get("last_seen") == NOW
    assert am.send(alert) is False
    assert len(pushes) == 1
    assert am.resolve(row["id"])
    assert am.send(alert) is True
    assert len(pushes) == 2
    assert db.get_connection().execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 2


@pytest.mark.parametrize("present", [(), ("last_escalated_at",), ("last_seen",)])
def test_legacy_alerts_migrate_idempotently_without_stock_pool(present):
    database = Database(":memory:")
    try:
        conn = database.get_connection()
        legacy = OPS_TABLES_DDL.replace("    last_escalated_at TEXT,\n", "").replace("    last_seen TEXT,\n", "")
        conn.executescript(legacy)
        event_id = seed(database)
        for column in present:
            conn.execute(f"ALTER TABLE alerts ADD COLUMN {column} TEXT")
            conn.execute(f"UPDATE alerts SET {column} = ?", (OLD,))
        conn.commit()
        before = dict(conn.execute("SELECT * FROM alerts").fetchone())
        result = ensure_schema(database)
        columns = {r[1]: r for r in conn.execute("PRAGMA table_info(alerts)")}
        assert {"last_seen", "last_escalated_at"} <= columns.keys()
        for column in ("last_seen", "last_escalated_at"):
            assert columns[column][2] == "TEXT"
            assert columns[column][3] == 0  # nullable, no fabricated history
        after = dict(conn.execute("SELECT * FROM alerts WHERE id=?", (event_id,)).fetchone())
        assert all(after[k] == v for k, v in before.items())
        for column in {"last_seen", "last_escalated_at"} - set(present):
            assert after[column] is None
        assert result["version_after"] == TARGET_VERSION
        assert ensure_schema(database)["applied"] == []
    finally:
        database.close()


def test_ops_start_upgrades_legacy_alerts(db, monkeypatch):
    conn = db.get_connection()
    conn.execute("ALTER TABLE alerts DROP COLUMN last_escalated_at")
    if "last_seen" in {r[1] for r in conn.execute("PRAGMA table_info(alerts)")}:
        conn.execute("ALTER TABLE alerts DROP COLUMN last_seen")
    conn.commit()
    ops = OpsLayer(db=db)
    monkeypatch.setattr(ops.version_tracker, "record_version", lambda: None)
    try:
        ops.start()
        columns = {r[1] for r in conn.execute("PRAGMA table_info(alerts)")}
        assert {"last_seen", "last_escalated_at"} <= columns
    finally:
        ops.orchestrator.shutdown()


@pytest.mark.parametrize("resend", [False, True])
def test_standalone_guard_upgrades_legacy_alerts(db, monkeypatch, resend):
    from ops_layer import run_guard

    conn = db.get_connection()
    conn.execute("ALTER TABLE alerts DROP COLUMN last_escalated_at")
    conn.execute("ALTER TABLE alerts DROP COLUMN last_seen")
    conn.commit()
    monkeypatch.setattr(run_guard, "_get_db", lambda: db)
    for detail in ("first observation", "latest observation"):
        if resend:
            run_guard._resend_alert({"name": "fixture", "severity": "critical", "detail": detail})
        else:
            run_guard._send_alert("fixture", "critical", detail)
    rows = conn.execute("SELECT * FROM alerts").fetchall()
    assert len(rows) == 1
    assert rows[0]["detail"] == "latest observation"
    assert rows[0]["last_seen"] == NOW
    assert {"last_seen", "last_escalated_at"} <= {
        r[1] for r in conn.execute("PRAGMA table_info(alerts)")}


@pytest.mark.parametrize("avail,expected", [(50, "resolved"), (30, "open"), (10, "open")])
def test_disk_recovers_only_monitor_titles_when_fully_healthy(db, monkeypatch, avail, expected):
    # 200 GiB total: 75% healthy / 85% warning / 95% critical.
    monkeypatch.setattr("os.statvfs", lambda _: SimpleNamespace(
        f_blocks=200, f_bavail=avail, f_frsize=2**30))
    monitored = [seed(db, title=t) for t in
                 ("磁盘空间不足", "磁盘空间严重不足", "磁盘空间偏高")]
    other = seed(db, title="数据库备份被跳过: 磁盘空间不足")
    foreign = seed(db, title="磁盘空间不足", layer="data")
    dismissed = seed(db, title="磁盘空间不足", status="dismissed")
    orch = PipelineOrchestrator(db, PipelineLogger(db), AlertManager(db))
    try:
        for _ in range(2):
            assert orch._run_disk_check({"ops.disk_warning_pct": 85,
                                       "ops.disk_alert_pct": 92,
                                       "ops.disk_alert_floor_gb": 20}).success
        rows = {r["id"]: dict(r) for r in db.get_connection().execute("SELECT * FROM alerts")}
        assert all(rows[i]["status"] == expected for i in monitored)
        if expected == "resolved":
            assert all(rows[i]["resolved_at"] == NOW for i in monitored)
            assert all(rows[i]["detail"] == "old observation" for i in monitored)
        assert rows[other]["status"] == "open"  # disk health is NOT backup success
        assert rows[foreign]["status"] == "open"
        assert rows[dismissed]["status"] == "dismissed"
    finally:
        orch.shutdown()
