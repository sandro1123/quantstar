import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from scripts import build_bj_shadow_merge as mod


FORMAL_VALUATION_DDL = """
CREATE TABLE stock_valuation (
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    pe_ttm REAL DEFAULT 0,
    pb_mrq REAL DEFAULT 0,
    ps_ttm REAL DEFAULT 0,
    market_cap REAL DEFAULT 0,
    total_shares REAL DEFAULT 0,
    dividend_yield REAL,
    PRIMARY KEY(code,date)
)
"""
CANDIDATE_VALUATION_DDL = """
CREATE TABLE stock_valuation (
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    pe_ttm REAL,
    pb_mrq REAL,
    ps_ttm REAL,
    market_cap REAL,
    total_shares REAL,
    dividend_yield REAL,
    PRIMARY KEY(code,date)
)
"""
PROVENANCE_DDL = """
CREATE TABLE valuation_provenance (
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    request_id TEXT NOT NULL,
    source TEXT NOT NULL,
    source_column_map TEXT,
    raw_row_sha256 TEXT,
    normalized_record_sha256 TEXT,
    retrieved_at TEXT,
    adapter_sha256 TEXT,
    identity_snapshot_sha256 TEXT,
    mapping_snapshot_sha256 TEXT,
    normalization_version TEXT,
    PRIMARY KEY(code,date)
)
"""


def make_formal(path: Path):
    conn = sqlite3.connect(path)
    conn.execute(FORMAL_VALUATION_DDL)
    conn.executemany(
        "INSERT INTO stock_valuation VALUES (?,?,?,?,?,?,?,?)",
        [
            ("920000", "20260717", 1.0, 2.0, 3.0, 0.0, 0.0, None),
            ("600000", "20260717", 10.0, 20.0, 30.0, 40.0, 50.0, 0.5),
        ],
    )
    conn.commit()
    conn.close()


def make_candidate(path: Path, code: str = "920000", drift_schema: bool = False):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    if drift_schema:
        conn.execute("CREATE TABLE stock_valuation(code TEXT, date TEXT, pe_ttm REAL, PRIMARY KEY(code,date))")
    else:
        conn.execute(CANDIDATE_VALUATION_DDL)
    conn.execute(PROVENANCE_DDL)
    if not drift_schema:
        rows = [
            (code, "2026-07-17", 1.1, 2.1, 3.1, 100.0, 10.0, None),
            (code, "2026-07-18", 1.2, 2.2, 3.2, 110.0, 10.0, None),
        ]
        conn.executemany("INSERT INTO stock_valuation VALUES (?,?,?,?,?,?,?,?)", rows)
        conn.executemany(
            "INSERT INTO valuation_provenance VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (code, date, f"rid-{i}", "source", "{}", "a" * 64, "b" * 64, "2026-07-18T00:00:00Z", "c" * 64, "d" * 64, "e" * 64, "v1")
                for i, (_, date, *_) in enumerate(rows)
            ],
        )
    conn.commit()
    conn.close()


def make_identity(path: Path, code: str = "920000"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"securities": [{"code": code}]}), encoding="utf-8")


def seal_candidate_root(root: Path, candidate: Path):
    manifests = root / "manifests"
    manifests.mkdir(parents=True)
    manifest = manifests / "manifest.json"
    payload_before_manifest = [p for p in root.rglob("*") if p.is_file()]
    manifest.write_text(
        json.dumps({"task": "fixture", "total_files": len(payload_before_manifest)}),
        encoding="utf-8",
    )
    payload = [p for p in root.rglob("*") if p.is_file() and p.name != "final.sha256"]
    entries = [f"{sha(path)}  {path.relative_to(root).as_posix()}" for path in sorted(payload)]
    (manifests / "final.sha256").write_text("\n".join(entries) + "\n", encoding="utf-8")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_formal_integrity_state_ignores_only_sidecar_mtime():
    before = {
        "db": {"exists": True, "size": 10, "inode": 1, "mtime_ns": 5, "sha256": "a"},
        "wal": {"exists": True, "size": 0, "inode": 2, "mtime_ns": 5, "sha256": "b"},
        "shm": {"exists": True, "size": 32, "inode": 3, "mtime_ns": 5, "sha256": "c"},
    }
    after = json.loads(json.dumps(before))
    after["wal"]["mtime_ns"] = 6
    after["shm"]["mtime_ns"] = 7
    mod.require_formal_integrity_unchanged(before, after, "test")
    after["shm"]["sha256"] = "changed"
    with pytest.raises(RuntimeError, match="integrity changed"):
        mod.require_formal_integrity_unchanged(before, after, "test")


def test_run_merges_normalized_conflict_and_preserves_non_bj(tmp_path, monkeypatch):
    formal = tmp_path / "formal.db"
    candidate_root = tmp_path / "candidate-root"
    candidate = candidate_root / "build/candidate.db"
    identity = candidate_root / "identity/identity.json"
    evidence = tmp_path / "evidence"
    make_formal(formal)
    make_candidate(candidate)
    make_identity(identity)
    seal_candidate_root(candidate_root, candidate)
    formal_before = sha(formal)
    monkeypatch.setattr(mod, "git_state", lambda repo: {"branch": "test", "head": "a" * 40, "status_count": 0})

    result = mod.run(formal, candidate, candidate_root, identity, evidence, tmp_path)

    assert result["status"] == "PASS_SHADOW_MERGE_BUILT"
    assert result["deleted_conflicts"] == 1
    assert result["rows"] == 3
    assert result["provenance_rows"] == 2
    assert sha(formal) == formal_before
    assert not (evidence / ".build.lock").exists()
    assert not (evidence / "shadow/qs-shadow.db.partial").exists()
    shadow = sqlite3.connect(evidence / "shadow/qs-shadow.db")
    try:
        assert shadow.execute("SELECT COUNT(*) FROM stock_valuation").fetchone()[0] == 3
        assert shadow.execute("SELECT pe_ttm FROM stock_valuation WHERE code='920000' AND date='2026-07-17'").fetchone()[0] == 1.1
        assert shadow.execute("SELECT COUNT(*) FROM stock_valuation WHERE code='920000' AND date='20260717'").fetchone()[0] == 0
        assert shadow.execute("SELECT pe_ttm FROM stock_valuation WHERE code='600000'").fetchone()[0] == 10.0
        assert shadow.execute("SELECT COUNT(*) FROM valuation_provenance").fetchone()[0] == 2
    finally:
        shadow.close()
    report = json.loads((evidence / "analysis/merge-report.json").read_text())
    assert report["normalized_overlap_rows"] == 1
    assert report["value_conflict_rows"] == 1
    assert report["exact_normalized_overlap_rows"] == 0
    assert report["formal_identity_rows_without_candidate_overlap"] == 0
    assert len(report["conflicts"]) == 1
    assert report["conflicts"][0]["differing_columns"]
    assert report["non_identity_hash_before"] == report["non_identity_hash_after"]
    assert report["formal_integrity_unchanged"] is True


def test_candidate_code_outside_identity_fails_before_snapshot(tmp_path, monkeypatch):
    formal = tmp_path / "formal.db"
    candidate_root = tmp_path / "candidate-root"
    candidate = candidate_root / "build/candidate.db"
    identity = candidate_root / "identity/identity.json"
    evidence = tmp_path / "evidence"
    make_formal(formal)
    make_candidate(candidate, code="920001")
    make_identity(identity, code="920000")
    seal_candidate_root(candidate_root, candidate)
    monkeypatch.setattr(mod, "git_state", lambda repo: {})

    with pytest.raises(ValueError, match="candidate code set differs"):
        mod.run(formal, candidate, candidate_root, identity, evidence, tmp_path)

    assert (evidence / ".build.lock").exists()
    assert not (evidence / "shadow/qs-shadow.db").exists()
    assert not (evidence / "shadow/qs-shadow.db.partial").exists()
    failure = json.loads((evidence / "analysis/failure.json").read_text())
    assert failure["status"] == "SHADOW_MERGE_FAILED"
    assert failure["published_exists"] is False
    assert failure["lock_retained"] is True


def test_candidate_schema_drift_fails_closed(tmp_path, monkeypatch):
    formal = tmp_path / "formal.db"
    candidate_root = tmp_path / "candidate-root"
    candidate = candidate_root / "build/candidate.db"
    identity = candidate_root / "identity/identity.json"
    evidence = tmp_path / "evidence"
    make_formal(formal)
    make_candidate(candidate, drift_schema=True)
    make_identity(identity)
    seal_candidate_root(candidate_root, candidate)
    monkeypatch.setattr(mod, "git_state", lambda repo: {})

    with pytest.raises(ValueError, match="schema differs"):
        mod.run(formal, candidate, candidate_root, identity, evidence, tmp_path)

    assert (evidence / ".build.lock").exists()
    assert not (evidence / "shadow/qs-shadow.db").exists()


def test_existing_evidence_root_is_never_reused(tmp_path, monkeypatch):
    formal = tmp_path / "formal.db"
    candidate_root = tmp_path / "candidate-root"
    candidate = candidate_root / "build/candidate.db"
    identity = candidate_root / "identity/identity.json"
    evidence = tmp_path / "evidence"
    make_formal(formal)
    make_candidate(candidate)
    make_identity(identity)
    seal_candidate_root(candidate_root, candidate)
    evidence.mkdir()
    marker = evidence / "marker"
    marker.write_bytes(b"keep")
    monkeypatch.setattr(mod, "git_state", lambda repo: {})

    with pytest.raises(FileExistsError, match="already exists"):
        mod.run(formal, candidate, candidate_root, identity, evidence, tmp_path)

    assert marker.read_bytes() == b"keep"


def test_invalid_identity_code_fails_before_snapshot(tmp_path, monkeypatch):
    formal = tmp_path / "formal.db"
    candidate_root = tmp_path / "candidate-root"
    candidate = candidate_root / "build/candidate.db"
    identity = candidate_root / "identity/identity.json"
    evidence = tmp_path / "evidence"
    make_formal(formal)
    make_candidate(candidate)
    make_identity(identity, code="../bad")
    seal_candidate_root(candidate_root, candidate)
    monkeypatch.setattr(mod, "git_state", lambda repo: {})

    with pytest.raises(ValueError, match="invalid BJ identity code"):
        mod.run(formal, candidate, candidate_root, identity, evidence, tmp_path)

    assert not (evidence / "shadow/qs-shadow.db").exists()


def test_tampered_sealed_candidate_fails_before_snapshot(tmp_path, monkeypatch):
    formal = tmp_path / "formal.db"
    candidate_root = tmp_path / "candidate-root"
    candidate = candidate_root / "build/candidate.db"
    identity = candidate_root / "identity/identity.json"
    evidence = tmp_path / "evidence"
    make_formal(formal)
    make_candidate(candidate)
    make_identity(identity)
    seal_candidate_root(candidate_root, candidate)
    candidate.write_bytes(candidate.read_bytes() + b"tamper")
    monkeypatch.setattr(mod, "git_state", lambda repo: {})

    with pytest.raises(ValueError, match="SHA mismatch"):
        mod.run(formal, candidate, candidate_root, identity, evidence, tmp_path)

    assert (evidence / ".build.lock").exists()
    assert not (evidence / "shadow/qs-shadow.db.partial").exists()
    assert not (evidence / "shadow/qs-shadow.db").exists()


def test_formal_drift_during_snapshot_blocks_publish(tmp_path, monkeypatch):
    formal = tmp_path / "formal.db"
    candidate_root = tmp_path / "candidate-root"
    candidate = candidate_root / "build/candidate.db"
    identity = candidate_root / "identity/identity.json"
    evidence = tmp_path / "evidence"
    make_formal(formal)
    make_candidate(candidate)
    make_identity(identity)
    seal_candidate_root(candidate_root, candidate)
    monkeypatch.setattr(mod, "git_state", lambda repo: {})
    original = mod.create_consistent_snapshot

    def snapshot_then_drift(source, target):
        original(source, target)
        conn = sqlite3.connect(source)
        conn.execute(
            "INSERT INTO stock_valuation VALUES (?,?,?,?,?,?,?,?)",
            ("600001", "20260717", 1.0, 1.0, 1.0, 1.0, 1.0, None),
        )
        conn.commit()
        conn.close()

    monkeypatch.setattr(mod, "create_consistent_snapshot", snapshot_then_drift)

    with pytest.raises(RuntimeError, match="integrity changed while creating"):
        mod.run(formal, candidate, candidate_root, identity, evidence, tmp_path)

    assert (evidence / ".build.lock").exists()
    assert (evidence / "shadow/qs-shadow.db.partial").exists()
    assert not (evidence / "shadow/qs-shadow.db").exists()


def test_semantically_invalid_candidate_date_fails_closed(tmp_path, monkeypatch):
    formal = tmp_path / "formal.db"
    candidate_root = tmp_path / "candidate-root"
    candidate = candidate_root / "build/candidate.db"
    identity = candidate_root / "identity/identity.json"
    evidence = tmp_path / "evidence"
    make_formal(formal)
    make_candidate(candidate)
    conn = sqlite3.connect(candidate)
    conn.execute("UPDATE stock_valuation SET date='2026-99-99' WHERE date='2026-07-18'")
    conn.execute("UPDATE valuation_provenance SET date='2026-99-99' WHERE date='2026-07-18'")
    conn.commit()
    conn.close()
    make_identity(identity)
    seal_candidate_root(candidate_root, candidate)
    monkeypatch.setattr(mod, "git_state", lambda repo: {})

    with pytest.raises(ValueError, match="invalid_dates=1"):
        mod.run(formal, candidate, candidate_root, identity, evidence, tmp_path)

    assert (evidence / ".build.lock").exists()
    assert not (evidence / "shadow/qs-shadow.db.partial").exists()
    assert not (evidence / "shadow/qs-shadow.db").exists()


def test_nonfinite_candidate_value_fails_closed(tmp_path, monkeypatch):
    formal = tmp_path / "formal.db"
    candidate_root = tmp_path / "candidate-root"
    candidate = candidate_root / "build/candidate.db"
    identity = candidate_root / "identity/identity.json"
    evidence = tmp_path / "evidence"
    make_formal(formal)
    make_candidate(candidate)
    conn = sqlite3.connect(candidate)
    conn.execute("UPDATE stock_valuation SET pe_ttm=? WHERE date='2026-07-18'", (float("inf"),))
    conn.commit()
    conn.close()
    make_identity(identity)
    seal_candidate_root(candidate_root, candidate)
    monkeypatch.setattr(mod, "git_state", lambda repo: {})

    with pytest.raises(ValueError, match="nonfinite_values=1"):
        mod.run(formal, candidate, candidate_root, identity, evidence, tmp_path)

    assert (evidence / ".build.lock").exists()
    assert not (evidence / "shadow/qs-shadow.db.partial").exists()
    assert not (evidence / "shadow/qs-shadow.db").exists()


def test_invalid_formal_identity_date_blocks_merge(tmp_path, monkeypatch):
    formal = tmp_path / "formal.db"
    candidate_root = tmp_path / "candidate-root"
    candidate = candidate_root / "build/candidate.db"
    identity = candidate_root / "identity/identity.json"
    evidence = tmp_path / "evidence"
    make_formal(formal)
    conn = sqlite3.connect(formal)
    conn.execute("UPDATE stock_valuation SET date='20269999' WHERE code='920000'")
    conn.commit()
    conn.close()
    make_candidate(candidate)
    make_identity(identity)
    seal_candidate_root(candidate_root, candidate)
    monkeypatch.setattr(mod, "git_state", lambda repo: {})

    with pytest.raises(ValueError, match="formal BJ date invariants failed"):
        mod.run(formal, candidate, candidate_root, identity, evidence, tmp_path)

    assert (evidence / ".build.lock").exists()
    assert (evidence / "shadow/qs-shadow.db.partial").exists()
    assert not (evidence / "shadow/qs-shadow.db").exists()


def test_ambiguous_formal_normalized_date_blocks_merge(tmp_path, monkeypatch):
    formal = tmp_path / "formal.db"
    candidate_root = tmp_path / "candidate-root"
    candidate = candidate_root / "build/candidate.db"
    identity = candidate_root / "identity/identity.json"
    evidence = tmp_path / "evidence"
    make_formal(formal)
    conn = sqlite3.connect(formal)
    conn.execute(
        "INSERT INTO stock_valuation VALUES (?,?,?,?,?,?,?,?)",
        ("920000", "2026-07-17", 9.0, 9.0, 9.0, 9.0, 9.0, None),
    )
    conn.commit()
    conn.close()
    make_candidate(candidate)
    make_identity(identity)
    seal_candidate_root(candidate_root, candidate)
    monkeypatch.setattr(mod, "git_state", lambda repo: {})

    with pytest.raises(ValueError, match="formal BJ date invariants failed.*normalized_duplicates=1"):
        mod.run(formal, candidate, candidate_root, identity, evidence, tmp_path)

    assert (evidence / ".build.lock").exists()
    assert (evidence / "shadow/qs-shadow.db.partial").exists()
    assert not (evidence / "shadow/qs-shadow.db").exists()


def test_unexpected_sqlite_master_change_blocks_publish(tmp_path, monkeypatch):
    formal = tmp_path / "formal.db"
    candidate_root = tmp_path / "candidate-root"
    candidate = candidate_root / "build/candidate.db"
    identity = candidate_root / "identity/identity.json"
    evidence = tmp_path / "evidence"
    make_formal(formal)
    make_candidate(candidate)
    make_identity(identity)
    seal_candidate_root(candidate_root, candidate)
    monkeypatch.setattr(mod, "git_state", lambda repo: {})
    original = mod.merge_candidate

    def merge_then_change_master(shadow, candidate_db, identity_codes):
        result = original(shadow, candidate_db, identity_codes)
        shadow.execute("CREATE INDEX illegal_shadow_index ON stock_valuation(pe_ttm)")
        shadow.commit()
        return result

    monkeypatch.setattr(mod, "merge_candidate", merge_then_change_master)

    with pytest.raises(ValueError, match="sqlite_master changed"):
        mod.run(formal, candidate, candidate_root, identity, evidence, tmp_path)

    assert (evidence / ".build.lock").exists()
    assert (evidence / "shadow/qs-shadow.db.partial").exists()
    assert not (evidence / "shadow/qs-shadow.db").exists()


def test_completion_record_failure_demotes_published_shadow(tmp_path, monkeypatch):
    formal = tmp_path / "formal.db"
    candidate_root = tmp_path / "candidate-root"
    candidate = candidate_root / "build/candidate.db"
    identity = candidate_root / "identity/identity.json"
    evidence = tmp_path / "evidence"
    make_formal(formal)
    make_candidate(candidate)
    make_identity(identity)
    seal_candidate_root(candidate_root, candidate)
    monkeypatch.setattr(mod, "git_state", lambda repo: {})
    original = mod.write_json_no_clobber

    def fail_completion(path, payload):
        if path.name == "build-result.json":
            raise OSError("injected completion record failure")
        return original(path, payload)

    monkeypatch.setattr(mod, "write_json_no_clobber", fail_completion)

    with pytest.raises(OSError, match="injected completion record failure"):
        mod.run(formal, candidate, candidate_root, identity, evidence, tmp_path)

    assert (evidence / ".build.lock").exists()
    assert (evidence / "shadow/qs-shadow.db.partial").exists()
    assert not (evidence / "shadow/qs-shadow.db").exists()
    assert not (evidence / "analysis/build-result.json").exists()


def test_publish_failure_retains_lock_and_partial(tmp_path, monkeypatch):
    formal = tmp_path / "formal.db"
    candidate_root = tmp_path / "candidate-root"
    candidate = candidate_root / "build/candidate.db"
    identity = candidate_root / "identity/identity.json"
    evidence = tmp_path / "evidence"
    make_formal(formal)
    make_candidate(candidate)
    make_identity(identity)
    seal_candidate_root(candidate_root, candidate)
    monkeypatch.setattr(mod, "git_state", lambda repo: {})

    def fail_publish(source, target):
        raise OSError("injected publish failure")

    monkeypatch.setattr(mod, "publish_no_clobber", fail_publish)

    with pytest.raises(OSError, match="injected publish failure"):
        mod.run(formal, candidate, candidate_root, identity, evidence, tmp_path)

    assert (evidence / ".build.lock").exists()
    assert (evidence / "shadow/qs-shadow.db.partial").exists()
    assert not (evidence / "shadow/qs-shadow.db").exists()
    ready = json.loads((evidence / "analysis/pre-publish.json").read_text())
    assert ready["status"] == "READY_TO_PUBLISH_SHADOW"
    assert not (evidence / "analysis/build-result.json").exists()
