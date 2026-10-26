import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from scripts import audit_bj_candidate_aware_merge as mod

FORMAL_DDL = """
CREATE TABLE stock_valuation (
 code TEXT NOT NULL, date TEXT NOT NULL,
 pe_ttm REAL DEFAULT 0, pb_mrq REAL DEFAULT 0, ps_ttm REAL DEFAULT 0,
 market_cap REAL DEFAULT 0, total_shares REAL DEFAULT 0, dividend_yield REAL,
 PRIMARY KEY(code,date))
"""
CANDIDATE_DDL = """
CREATE TABLE stock_valuation (
 code TEXT NOT NULL, date TEXT NOT NULL,
 pe_ttm REAL, pb_mrq REAL, ps_ttm REAL,
 market_cap REAL, total_shares REAL, dividend_yield REAL,
 PRIMARY KEY(code,date))
"""
PROVENANCE_DDL = """
CREATE TABLE valuation_provenance (
 code TEXT NOT NULL, date TEXT NOT NULL, request_id TEXT NOT NULL, source TEXT NOT NULL,
 source_column_map TEXT, raw_row_sha256 TEXT, normalized_record_sha256 TEXT,
 retrieved_at TEXT, adapter_sha256 TEXT, identity_snapshot_sha256 TEXT,
 mapping_snapshot_sha256 TEXT, normalization_version TEXT,
 PRIMARY KEY(code,date))
"""
VALUATION_ROW = ("920000", "2026-08-01", 10.0, 2.0, 3.0, 100.0, 10.0, 0.5)
PRESERVED_ROW = ("600000", "2026-08-01", 20.0, 3.0, 4.0, 200.0, 20.0, 0.6)
PROVENANCE_ROW = (
    "920000", "2026-08-01", "req-1", "fixture", "{}", "a" * 64, "b" * 64,
    "2026-08-02T00:00:00Z", "c" * 64, "d" * 64, "e" * 64, "v1",
)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def seal(root: Path, task: str) -> None:
    manifests = root / "manifests"
    manifests.mkdir(parents=True, exist_ok=True)
    final = manifests / "final.sha256"
    manifest = manifests / "manifest.json"
    if final.exists():
        final.unlink()
    if manifest.exists():
        manifest.unlink()
    payload = sorted(
        (p for p in root.rglob("*") if p.is_file() and p not in (final, manifest)),
        key=lambda p: p.relative_to(root).as_posix(),
    )
    manifest.write_text(json.dumps({
        "task": task,
        "seal_contract": "final_sha256_includes_manifest_json_v1",
        "total_files": len(payload),
        "total_size_bytes": sum(p.stat().st_size for p in payload),
    }), encoding="utf-8")
    listed = payload + [manifest]
    final.write_text("".join(f"{sha(p)}  {p.relative_to(root).as_posix()}\n" for p in listed), encoding="utf-8")


def insert_valuation(connection: sqlite3.Connection, row: tuple):
    connection.execute("INSERT INTO stock_valuation VALUES (?,?,?,?,?,?,?,?)", row)


def make_fixture(tmp_path: Path):
    formal = tmp_path / "formal.db"
    outer = tmp_path / "outer"
    shadow = outer / "shadow/qs-shadow.db"
    inner = outer / "inputs/sealed-candidate"
    candidate = inner / "build/stage-b-bj-valuation.db"
    identity = inner / "identity/bj-active-securities.json"
    shadow.parent.mkdir(parents=True)
    candidate.parent.mkdir(parents=True)
    identity.parent.mkdir(parents=True)

    connection = sqlite3.connect(formal)
    connection.execute(FORMAL_DDL)
    old_identity = list(VALUATION_ROW)
    old_identity[2] = 9.0
    old_identity[1] = "20260801"
    insert_valuation(connection, tuple(old_identity))
    insert_valuation(connection, PRESERVED_ROW)
    connection.commit()
    connection.close()

    connection = sqlite3.connect(candidate)
    connection.execute(CANDIDATE_DDL)
    connection.execute(PROVENANCE_DDL)
    insert_valuation(connection, VALUATION_ROW)
    connection.execute("INSERT INTO valuation_provenance VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", PROVENANCE_ROW)
    connection.commit()
    connection.close()
    identity.write_text(json.dumps({"securities": [{"code": "920000", "market": "北交所"}]}), encoding="utf-8")
    seal(inner, "inner")

    connection = sqlite3.connect(shadow)
    connection.execute(FORMAL_DDL)
    connection.execute(PROVENANCE_DDL)
    insert_valuation(connection, VALUATION_ROW)
    insert_valuation(connection, PRESERVED_ROW)
    connection.execute("INSERT INTO valuation_provenance VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", PROVENANCE_ROW)
    connection.commit()
    connection.close()
    (outer / "analysis").mkdir()
    (outer / "analysis/build-result.json").write_text("{}", encoding="utf-8")
    seal(outer, "outer")
    return formal, outer, shadow, inner, candidate, identity


def inputs(tmp_path: Path, **overrides):
    formal, outer, shadow, inner, candidate, identity = make_fixture(tmp_path)
    values = dict(
        formal_db=formal,
        shadow_root=outer,
        shadow_db=shadow,
        candidate_root=inner,
        candidate_db=candidate,
        identity=identity,
        evidence_root=tmp_path / "evidence",
        expected_formal_sha256=sha(formal),
        expected_shadow_sha256=sha(shadow),
        expected_candidate_sha256=sha(candidate),
        timeout_seconds=10.0,
        max_samples=3,
    )
    values.update(overrides)
    return mod.Inputs(**values), (formal, outer, shadow, inner, candidate, identity)


def reseal_both(outer: Path, inner: Path):
    seal(inner, "inner")
    seal(outer, "outer")


def mutate_db(path: Path, sql: str, params=()):
    connection = sqlite3.connect(path)
    connection.execute(sql, params)
    connection.commit()
    connection.close()


def test_pass_proves_expected_replacement_preservation_and_provenance(tmp_path):
    config, paths = inputs(tmp_path)
    before = {str(path): sha(path) for path in (paths[0], paths[2], paths[4])}
    result = mod.run(config)
    assert result["verdict"] == "PASS_CANDIDATE_AWARE_SHADOW_EXPECTATION_PROVEN"
    assert result["counts"]["expected_replacement_rows"] == 1
    assert result["counts"]["formal_preserved_rows"] == 1
    assert result["counts"]["rows"]["shadow_total"] == 2
    assert result["anomalies"] == {}
    assert not (config.evidence_root / ".audit.lock").exists()
    assert {str(path): sha(path) for path in (paths[0], paths[2], paths[4])} == before


@pytest.mark.parametrize(
    "mutation,gate",
    [
        (lambda shadow: mutate_db(shadow, "UPDATE stock_valuation SET pe_ttm=99 WHERE code='920000'"), "identity_shadow_minus_candidate"),
        (lambda shadow: mutate_db(shadow, "DELETE FROM stock_valuation WHERE code='920000'"), "identity_candidate_minus_shadow"),
        (lambda shadow: mutate_db(shadow, "UPDATE stock_valuation SET pe_ttm=99 WHERE code='600000'"), "preserved_shadow_minus_formal"),
        (lambda shadow: mutate_db(shadow, "INSERT INTO stock_valuation VALUES ('600001','2026-08-01',1,1,1,1,1,1)"), "preserved_shadow_minus_formal"),
        (lambda shadow: mutate_db(shadow, "DELETE FROM valuation_provenance WHERE code='920000'"), "provenance_candidate_minus_shadow"),
        (lambda shadow: mutate_db(shadow, "UPDATE valuation_provenance SET source='tampered' WHERE code='920000'"), "provenance_shadow_minus_candidate"),
    ],
)
def test_business_anomalies_are_detected_after_valid_reseal(tmp_path, mutation, gate):
    config, paths = inputs(tmp_path)
    mutation(paths[2])
    reseal_both(paths[1], paths[3])
    config = mod.Inputs(**{**config.__dict__, "expected_shadow_sha256": sha(paths[2])})
    with pytest.raises(mod.AuditError, match="FAIL_CANDIDATE_AWARE_SHADOW_ANOMALY"):
        mod.run(config)
    verdict = json.loads((config.evidence_root / "analysis/candidate-aware-verdict.json").read_text())
    failed = {item["gate"] for item in verdict["gates"] if not item["passed"]}
    assert gate in failed
    assert verdict["anomalies"][gate]["count"] >= 1
    assert len(verdict["anomalies"][gate]["sample"]) <= config.max_samples
    assert len(verdict["anomalies"][gate]["digest"]) == 64
    assert (config.evidence_root / ".audit.lock").exists()
    assert (config.evidence_root / "analysis/failure.json").exists()


def test_provenance_out_of_scope_is_detected(tmp_path):
    config, paths = inputs(tmp_path)
    extra = list(PROVENANCE_ROW)
    extra[0] = "600000"
    mutate_db(paths[2], "INSERT INTO valuation_provenance VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", tuple(extra))
    reseal_both(paths[1], paths[3])
    config = mod.Inputs(**{**config.__dict__, "expected_shadow_sha256": sha(paths[2])})
    with pytest.raises(mod.AuditError):
        mod.run(config)
    verdict = json.loads((config.evidence_root / "analysis/candidate-aware-verdict.json").read_text())
    assert verdict["counts"]["shadow_provenance_out_of_scope"] == 1


def test_formal_identity_key_without_candidate_is_rejected(tmp_path):
    config, paths = inputs(tmp_path)
    extra = list(VALUATION_ROW)
    extra[1] = "20260731"
    mutate_db(paths[0], "INSERT INTO stock_valuation VALUES (?,?,?,?,?,?,?,?)", tuple(extra))
    config = mod.Inputs(**{**config.__dict__, "expected_formal_sha256": sha(paths[0])})
    with pytest.raises(mod.AuditError):
        mod.run(config)
    verdict = json.loads((config.evidence_root / "analysis/candidate-aware-verdict.json").read_text())
    assert verdict["counts"]["formal_identity_without_candidate_key"] == 1


def test_tampered_inner_or_outer_seal_fails_before_sql_audit(tmp_path):
    config, paths = inputs(tmp_path)
    paths[5].write_text('{"securities":[]}', encoding="utf-8")
    with pytest.raises(mod.AuditError, match="SHA mismatch"):
        mod.run(config)
    assert not (config.evidence_root / "analysis/candidate-aware-verdict.json").exists()
    failure = json.loads((config.evidence_root / "analysis/failure.json").read_text())
    assert failure["error_type"] == "AuditError"


def test_expected_sha_pin_and_nonempty_wal_fail_before_sql_audit(tmp_path):
    config, paths = inputs(tmp_path)
    bad_pin = mod.Inputs(**{**config.__dict__, "expected_formal_sha256": "0" * 64})
    with pytest.raises(mod.AuditError, match="formal SHA mismatch"):
        mod.run(bad_pin)
    assert not (bad_pin.evidence_root / "analysis/candidate-aware-verdict.json").exists()

    second_root = tmp_path / "evidence-wal"
    Path(str(paths[2]) + "-wal").write_bytes(b"not-checkpointed")
    wal_config = mod.Inputs(**{**config.__dict__, "evidence_root": second_root})
    with pytest.raises(mod.AuditError, match="non-empty WAL"):
        mod.run(wal_config)
    assert not (second_root / "analysis/candidate-aware-verdict.json").exists()


def test_timeout_is_synchronous_and_does_not_publish_verdict(tmp_path, monkeypatch):
    config, _ = inputs(tmp_path)
    config = mod.Inputs(**{**config.__dict__, "timeout_seconds": 0.000001})
    with pytest.raises(mod.AuditTimeoutError):
        mod.run(config)
    assert not (config.evidence_root / "analysis/candidate-aware-verdict.json").exists()
    assert (config.evidence_root / ".audit.lock").exists()


def test_no_clobber_existing_evidence_root(tmp_path):
    config, _ = inputs(tmp_path)
    config.evidence_root.mkdir()
    marker = config.evidence_root / "marker"
    marker.write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError):
        mod.run(config)
    assert marker.read_text() == "keep"


def test_symlink_input_is_rejected(tmp_path):
    config, paths = inputs(tmp_path)
    link = tmp_path / "shadow-link.db"
    try:
        link.symlink_to(paths[2])
    except OSError:
        pytest.skip("symlinks unavailable")
    config = mod.Inputs(**{**config.__dict__, "shadow_db": link})
    with pytest.raises(mod.AuditError, match="non-symlink"):
        mod.run(config)
