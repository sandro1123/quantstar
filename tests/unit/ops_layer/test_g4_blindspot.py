"""G4 盲区补偿 — promote_fallback_entries scoped 单测 (隔离 tmp 目录, 不碰生产)。"""

import json

from ops_layer import g4_blindspot as bs


class _FakeDB:
    def __init__(self):
        import sqlite3
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("""CREATE TABLE alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, severity TEXT, layer TEXT,
            title TEXT, detail TEXT, action TEXT, status TEXT DEFAULT 'open',
            resolved_at TEXT, created_at TEXT)""")
        self.fail = False

    def get_connection(self):
        if self.fail:
            raise RuntimeError("db down")
        return self.conn


def test_promotes_failed_pipeline_run_entry(tmp_path, monkeypatch):
    fb = tmp_path / "observability_fallback.jsonl"
    st = tmp_path / "g4_blindspot_state.json"
    entry = {"kind": "pipeline_run", "name": "e2e_verification", "status": "failed",
             "error": "exit code 1", "version": "257dc9b", "config_hash": "x",
             "reason": "database locked after 3 attempts: database is locked",
             "fallback_ts": "2026-08-24 18:00:22", "recovered": False}
    fb.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    monkeypatch.setattr(bs, "_FALLBACK_PATH", str(fb))
    monkeypatch.setattr(bs, "_STATE_PATH", str(st))

    db = _FakeDB()
    n = bs.promote_fallback_entries(db)
    assert n == 1
    row = db.conn.execute("SELECT title, severity FROM alerts").fetchone()
    assert "e2e_verification" in row["title"]
    assert row["severity"] == "warning"

    # 幂等: 同一条目不重复升格
    assert bs.promote_fallback_entries(_FakeDB()) == 0


def test_ignores_non_pipeline_or_success_entries(tmp_path, monkeypatch):
    fb = tmp_path / "fb.jsonl"
    st = tmp_path / "st.json"
    lines = [
        json.dumps({"kind": "other", "status": "failed"}),
        json.dumps({"kind": "pipeline_run", "name": "p", "status": "success",
                    "fallback_ts": "t1"}),
    ]
    fb.write_text("\n".join(lines), encoding="utf-8")
    monkeypatch.setattr(bs, "_FALLBACK_PATH", str(fb))
    monkeypatch.setattr(bs, "_STATE_PATH", str(st))
    assert bs.promote_fallback_entries(_FakeDB()) == 0


def test_db_failure_is_failsafe_and_retried_next_round(tmp_path, monkeypatch):
    fb = tmp_path / "fb.jsonl"
    st = tmp_path / "st.json"
    entry = {"kind": "pipeline_run", "name": "q", "status": "failed",
             "reason": "boom", "fallback_ts": "t2"}
    fb.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    monkeypatch.setattr(bs, "_FALLBACK_PATH", str(fb))
    monkeypatch.setattr(bs, "_STATE_PATH", str(st))

    db = _FakeDB()
    db.fail = True
    # 不抛异常即通过 (fail-safe)
    assert bs.promote_fallback_entries(db) == 0
    # 库恢复后可重试成功
    db.fail = False
    assert bs.promote_fallback_entries(db) == 1


def test_missing_file_returns_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(bs, "_FALLBACK_PATH", str(tmp_path / "nope.jsonl"))
    assert bs.promote_fallback_entries(_FakeDB()) == 0
