#!/usr/bin/env python3
"""Fail-closed candidate-aware audit for a formal/shadow/sealed-candidate merge."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

VALUATION_COLUMNS = (
    "code", "date", "pe_ttm", "pb_mrq", "ps_ttm", "market_cap",
    "total_shares", "dividend_yield",
)
PROVENANCE_COLUMNS = (
    "code", "date", "request_id", "source", "source_column_map",
    "raw_row_sha256", "normalized_record_sha256", "retrieved_at",
    "adapter_sha256", "identity_snapshot_sha256", "mapping_snapshot_sha256",
    "normalization_version",
)
FORMAL_VALUATION_SIGNATURE = [
    [0, "code", "TEXT", 1, None, 1], [1, "date", "TEXT", 1, None, 2],
    [2, "pe_ttm", "REAL", 0, "0", 0], [3, "pb_mrq", "REAL", 0, "0", 0],
    [4, "ps_ttm", "REAL", 0, "0", 0], [5, "market_cap", "REAL", 0, "0", 0],
    [6, "total_shares", "REAL", 0, "0", 0], [7, "dividend_yield", "REAL", 0, None, 0],
]
CANDIDATE_VALUATION_SIGNATURE = [
    [0, "code", "TEXT", 1, None, 1], [1, "date", "TEXT", 1, None, 2],
    [2, "pe_ttm", "REAL", 0, None, 0], [3, "pb_mrq", "REAL", 0, None, 0],
    [4, "ps_ttm", "REAL", 0, None, 0], [5, "market_cap", "REAL", 0, None, 0],
    [6, "total_shares", "REAL", 0, None, 0], [7, "dividend_yield", "REAL", 0, None, 0],
]
PROVENANCE_SIGNATURE = [
    [0, "code", "TEXT", 1, None, 1], [1, "date", "TEXT", 1, None, 2],
    [2, "request_id", "TEXT", 1, None, 0], [3, "source", "TEXT", 1, None, 0],
    [4, "source_column_map", "TEXT", 0, None, 0], [5, "raw_row_sha256", "TEXT", 0, None, 0],
    [6, "normalized_record_sha256", "TEXT", 0, None, 0], [7, "retrieved_at", "TEXT", 0, None, 0],
    [8, "adapter_sha256", "TEXT", 0, None, 0], [9, "identity_snapshot_sha256", "TEXT", 0, None, 0],
    [10, "mapping_snapshot_sha256", "TEXT", 0, None, 0], [11, "normalization_version", "TEXT", 0, None, 0],
]


class AuditError(RuntimeError):
    pass


class AuditTimeoutError(AuditError):
    pass


@dataclass(frozen=True)
class Inputs:
    formal_db: Path
    shadow_root: Path
    shadow_db: Path
    candidate_root: Path
    candidate_db: Path
    identity: Path
    evidence_root: Path
    expected_formal_sha256: str
    expected_shadow_sha256: str
    expected_candidate_sha256: str
    timeout_seconds: float = 900.0
    max_samples: int = 20


class Deadline:
    def __init__(self, seconds: float):
        if seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.started = time.monotonic()
        self.ends = self.started + seconds

    def check(self, stage: str) -> None:
        if time.monotonic() >= self.ends:
            raise AuditTimeoutError(f"candidate-aware audit timed out during {stage}")

    @property
    def elapsed_ms(self) -> float:
        return round((time.monotonic() - self.started) * 1000, 3)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path, deadline: Deadline | None = None, stage: str = "hash") -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            if deadline:
                deadline.check(stage)
            chunk = handle.read(8 * 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def file_state(path: Path) -> dict[str, Any]:
    state: dict[str, Any] = {"path": str(path), "exists": path.exists()}
    if path.exists():
        stat = path.stat()
        state.update(size=stat.st_size, inode=stat.st_ino, mtime_ns=stat.st_mtime_ns)
    return state


def db_state(path: Path) -> dict[str, Any]:
    return {
        "db": file_state(path),
        "wal": file_state(Path(str(path) + "-wal")),
        "shm": file_state(Path(str(path) + "-shm")),
    }


def integrity_projection(state: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, item in state.items():
        allowed = ("path", "exists", "size", "inode") if name in ("wal", "shm") else (
            "path", "exists", "size", "inode", "mtime_ns"
        )
        result[name] = {key: item.get(key) for key in allowed}
    return result


def write_json_no_clobber(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode()
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if fd >= 0:
            os.close(fd)


def _safe_manifest_entries(root: Path) -> tuple[Path, Path, list[tuple[str, str]]]:
    root = root.resolve()
    final_path = root / "manifests/final.sha256"
    manifest_path = root / "manifests/manifest.json"
    if not final_path.is_file() or not manifest_path.is_file():
        raise AuditError(f"evidence root is not sealed: {root}")
    entries: list[tuple[str, str]] = []
    for number, raw in enumerate(final_path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        parts = raw.split(None, 1)
        if len(parts) != 2:
            raise AuditError(f"invalid final.sha256 line {number}")
        digest, rel = parts[0].lower(), parts[1].strip().lstrip("*")
        relative = Path(rel)
        if (
            len(digest) != 64
            or any(ch not in "0123456789abcdef" for ch in digest)
            or relative.is_absolute()
            or ".." in relative.parts
            or relative.as_posix() in ("", "manifests/final.sha256")
        ):
            raise AuditError(f"unsafe final.sha256 entry: {rel!r}")
        entries.append((digest, relative.as_posix()))
    paths = [rel for _, rel in entries]
    if not entries or len(paths) != len(set(paths)):
        raise AuditError("final.sha256 is empty or contains duplicate paths")
    return final_path, manifest_path, entries


def verify_sealed_root(
    root: Path,
    required_relative_paths: Iterable[str],
    deadline: Deadline,
    label: str,
) -> dict[str, Any]:
    root = root.resolve()
    final_path, manifest_path, entries = _safe_manifest_entries(root)
    all_paths = list(root.rglob("*"))
    symlinks = [path.relative_to(root).as_posix() for path in all_paths if path.is_symlink()]
    locks = [path.relative_to(root).as_posix() for path in all_paths if path.name == ".seal.lock"]
    if symlinks or locks:
        raise AuditError(f"{label} seal contains symlinks or locks: {symlinks} {locks}")
    actual_files = {
        path.relative_to(root).as_posix()
        for path in all_paths
        if path.is_file() and path != final_path and path.name != ".seal.lock"
    }
    listed = {rel for _, rel in entries}
    if actual_files != listed:
        raise AuditError(
            f"{label} sealed file set mismatch: missing={sorted(listed-actual_files)} "
            f"unmanifested={sorted(actual_files-listed)}"
        )
    required = set(required_relative_paths) | {"manifests/manifest.json"}
    if not required.issubset(listed):
        raise AuditError(f"{label} seal misses required paths: {sorted(required-listed)}")
    mismatches = []
    for expected, rel in entries:
        deadline.check(f"{label} manifest verification")
        if sha256_file(root / rel, deadline, f"{label} payload hash") != expected:
            mismatches.append(rel)
    if mismatches:
        raise AuditError(f"{label} seal SHA mismatch: {mismatches}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("seal_contract") != "final_sha256_includes_manifest_json_v1":
        raise AuditError(f"{label} seal contract mismatch")
    if manifest.get("total_files") + 1 != len(entries):
        raise AuditError(f"{label} manifest count mismatch")
    return {
        "root": str(root),
        "payload_entries": len(entries),
        "manifest_sha256": sha256_file(manifest_path),
        "final_sha256_file_sha256": sha256_file(final_path),
        "required_paths": sorted(required),
    }


def validate_expected_sha256(value: str, label: str) -> str:
    normalized = value.lower()
    if len(normalized) != 64 or any(ch not in "0123456789abcdef" for ch in normalized):
        raise ValueError(f"{label} expected SHA256 must be 64 lowercase hex characters")
    return normalized


def require_checkpointed(path: Path, label: str) -> None:
    wal = Path(str(path) + "-wal")
    if wal.exists() and wal.stat().st_size > 0:
        raise AuditError(f"{label} has non-empty WAL; immutable audit is unsafe: {wal}")


def sealed_digest(root: Path, relative: str) -> str:
    _, _, entries = _safe_manifest_entries(root)
    matches = [digest for digest, rel in entries if rel == relative]
    if len(matches) != 1:
        raise AuditError(f"sealed root must bind {relative!r} exactly once")
    return matches[0]


def load_identity(identity: Path) -> list[str]:
    payload = json.loads(identity.read_text(encoding="utf-8"))
    securities = payload.get("securities") if isinstance(payload, dict) else None
    if not isinstance(securities, list):
        raise AuditError("identity securities must be a list")
    codes = []
    for item in securities:
        code = item.get("code") if isinstance(item, dict) else None
        if not isinstance(code, str) or len(code) != 6 or not code.isdigit() or not code.startswith("92"):
            raise AuditError(f"invalid BJ identity code: {code!r}")
        codes.append(code)
    if not codes or len(codes) != len(set(codes)):
        raise AuditError("identity codes must be non-empty and unique")
    return sorted(codes)


def connect_memory(deadline: Deadline) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row

    def progress() -> int:
        return 1 if time.monotonic() >= deadline.ends else 0

    connection.set_progress_handler(progress, 10_000)
    return connection


def attach_ro(connection: sqlite3.Connection, path: Path, name: str) -> None:
    # SQLite ATTACH 语句不接受 file:-URI(需 uri=True 的 connect); 用普通路径
    connection.execute(f"ATTACH DATABASE ? AS {name}", (path.resolve().as_posix(),))


def schema_signature(connection: sqlite3.Connection, schema: str, table: str) -> list[list[Any]]:
    return [list(row) for row in connection.execute(f"PRAGMA {schema}.table_info({table})")]


def count(connection: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    return int(connection.execute(sql, params).fetchone()[0])


def sample(connection: sqlite3.Connection, sql: str, limit: int) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(sql + f" LIMIT {int(limit)}")]


def sql_digest(connection: sqlite3.Connection, sql: str, deadline: Deadline) -> str:
    digest = hashlib.sha256()
    for row in connection.execute(sql):
        deadline.check("anomaly digest")
        digest.update(json.dumps(list(row), separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def audit(inputs: Inputs) -> dict[str, Any]:
    deadline = Deadline(inputs.timeout_seconds)
    raw_files = (inputs.formal_db, inputs.shadow_db, inputs.candidate_db, inputs.identity)
    for path in raw_files:
        if path.is_symlink() or not path.is_file():
            raise AuditError(f"input must be a regular non-symlink file: {path}")
    if inputs.shadow_root.is_symlink() or inputs.candidate_root.is_symlink():
        raise AuditError("sealed roots must not be symlinks")
    formal = inputs.formal_db.resolve()
    shadow_root = inputs.shadow_root.resolve()
    shadow = inputs.shadow_db.resolve()
    candidate_root = inputs.candidate_root.resolve()
    candidate = inputs.candidate_db.resolve()
    identity = inputs.identity.resolve()
    expected_formal = validate_expected_sha256(inputs.expected_formal_sha256, "formal")
    expected_shadow = validate_expected_sha256(inputs.expected_shadow_sha256, "shadow")
    expected_candidate = validate_expected_sha256(inputs.expected_candidate_sha256, "candidate")
    for path, label in ((formal, "formal"), (shadow, "shadow"), (candidate, "candidate")):
        require_checkpointed(path, label)
    for path, root, label in (
        (shadow, shadow_root, "shadow DB"),
        (candidate, candidate_root, "candidate DB"),
        (identity, candidate_root, "identity"),
    ):
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise AuditError(f"{label} must be inside its sealed root") from exc
    shadow_rel = shadow.relative_to(shadow_root).as_posix()
    candidate_rel = candidate.relative_to(candidate_root).as_posix()
    identity_rel = identity.relative_to(candidate_root).as_posix()
    before = {name: db_state(path) for name, path in (("formal", formal), ("shadow", shadow), ("candidate", candidate))}
    outer_seal = verify_sealed_root(
        shadow_root,
        (shadow_rel, f"inputs/sealed-candidate/{candidate_rel}", f"inputs/sealed-candidate/{identity_rel}"),
        deadline,
        "outer shadow",
    )
    inner_seal = verify_sealed_root(candidate_root, (candidate_rel, identity_rel), deadline, "inner candidate")
    sealed_shadow_sha = sealed_digest(shadow_root, shadow_rel)
    sealed_candidate_sha = sealed_digest(candidate_root, candidate_rel)
    if sealed_shadow_sha != expected_shadow:
        raise AuditError(f"shadow expected SHA differs from outer seal: {expected_shadow} != {sealed_shadow_sha}")
    if sealed_candidate_sha != expected_candidate:
        raise AuditError(f"candidate expected SHA differs from inner seal: {expected_candidate} != {sealed_candidate_sha}")
    actual_formal_sha = sha256_file(formal, deadline, "formal DB SHA pin")
    if actual_formal_sha != expected_formal:
        raise AuditError(f"formal SHA mismatch: expected={expected_formal} actual={actual_formal_sha}")
    codes = load_identity(identity)
    connection = connect_memory(deadline)
    gates: list[dict[str, Any]] = []
    counts: dict[str, Any] = {}
    anomaly_sql: dict[str, str] = {}
    valuations = ",".join(VALUATION_COLUMNS)
    provenance = ",".join(PROVENANCE_COLUMNS)
    try:
        attach_ro(connection, formal, "formal")
        attach_ro(connection, shadow, "shadow")
        attach_ro(connection, candidate, "candidate")
        connection.execute("CREATE TEMP TABLE identity(code TEXT PRIMARY KEY)")
        connection.executemany("INSERT INTO identity(code) VALUES (?)", ((code,) for code in codes))
        connection.commit()
        for schema in ("formal", "shadow", "candidate"):
            quick = connection.execute(f"PRAGMA {schema}.quick_check").fetchone()[0]
            gates.append({"gate": f"{schema}_quick_check", "passed": quick == "ok", "detail": quick})
        formal_sig = schema_signature(connection, "formal", "stock_valuation")
        shadow_sig = schema_signature(connection, "shadow", "stock_valuation")
        candidate_sig = schema_signature(connection, "candidate", "stock_valuation")
        prov_shadow_sig = schema_signature(connection, "shadow", "valuation_provenance")
        prov_candidate_sig = schema_signature(connection, "candidate", "valuation_provenance")
        gates.extend([
            {"gate": "formal_shadow_valuation_schema", "passed": formal_sig == shadow_sig == FORMAL_VALUATION_SIGNATURE, "detail": {"formal": formal_sig, "shadow": shadow_sig}},
            {"gate": "candidate_valuation_schema_exact", "passed": candidate_sig == CANDIDATE_VALUATION_SIGNATURE, "detail": candidate_sig},
            {"gate": "provenance_schema_exact", "passed": prov_shadow_sig == prov_candidate_sig == PROVENANCE_SIGNATURE, "detail": {"shadow": prov_shadow_sig, "candidate": prov_candidate_sig}},
        ])
        candidate_codes = [row[0] for row in connection.execute("SELECT DISTINCT code FROM candidate.stock_valuation ORDER BY code")]
        shadow_identity_codes = [row[0] for row in connection.execute("SELECT DISTINCT code FROM shadow.stock_valuation WHERE code IN (SELECT code FROM identity) ORDER BY code")]
        gates.append({"gate": "candidate_identity_code_set", "passed": candidate_codes == codes, "detail": {"identity": len(codes), "candidate": len(candidate_codes)}})
        gates.append({"gate": "shadow_identity_code_set", "passed": shadow_identity_codes == codes, "detail": {"identity": len(codes), "shadow": len(shadow_identity_codes)}})
        candidate_invalid_dates = count(connection, "SELECT COUNT(*) FROM candidate.stock_valuation WHERE date NOT GLOB '????-??-??' OR date(date) IS NULL OR date(date) != date")
        candidate_normalized_duplicates = count(connection, "SELECT COUNT(*) FROM (SELECT code,replace(date,'-','') normalized_date,COUNT(*) c FROM candidate.stock_valuation GROUP BY code,normalized_date HAVING c>1)")
        candidate_nonfinite = count(connection, "SELECT COUNT(*) FROM candidate.stock_valuation WHERE (pe_ttm IS NOT NULL AND (pe_ttm!=pe_ttm OR abs(pe_ttm)>1.7976931348623157e308)) OR (pb_mrq IS NOT NULL AND (pb_mrq!=pb_mrq OR abs(pb_mrq)>1.7976931348623157e308)) OR (ps_ttm IS NOT NULL AND (ps_ttm!=ps_ttm OR abs(ps_ttm)>1.7976931348623157e308)) OR (market_cap IS NOT NULL AND (market_cap!=market_cap OR abs(market_cap)>1.7976931348623157e308)) OR (total_shares IS NOT NULL AND (total_shares!=total_shares OR abs(total_shares)>1.7976931348623157e308)) OR (dividend_yield IS NOT NULL AND (dividend_yield!=dividend_yield OR abs(dividend_yield)>1.7976931348623157e308))")
        gates.extend([
            {"gate": "candidate_dates_canonical", "passed": candidate_invalid_dates == 0, "detail": candidate_invalid_dates},
            {"gate": "candidate_normalized_keys_unique", "passed": candidate_normalized_duplicates == 0, "detail": candidate_normalized_duplicates},
            {"gate": "candidate_numeric_values_finite", "passed": candidate_nonfinite == 0, "detail": candidate_nonfinite},
        ])
        queries = {
            "identity_shadow_minus_candidate": f"SELECT {valuations} FROM shadow.stock_valuation WHERE code IN (SELECT code FROM identity) EXCEPT SELECT {valuations} FROM candidate.stock_valuation",
            "identity_candidate_minus_shadow": f"SELECT {valuations} FROM candidate.stock_valuation EXCEPT SELECT {valuations} FROM shadow.stock_valuation WHERE code IN (SELECT code FROM identity)",
            "preserved_shadow_minus_formal": f"SELECT {valuations} FROM shadow.stock_valuation WHERE code NOT IN (SELECT code FROM identity) EXCEPT SELECT {valuations} FROM formal.stock_valuation WHERE code NOT IN (SELECT code FROM identity)",
            "preserved_formal_minus_shadow": f"SELECT {valuations} FROM formal.stock_valuation WHERE code NOT IN (SELECT code FROM identity) EXCEPT SELECT {valuations} FROM shadow.stock_valuation WHERE code NOT IN (SELECT code FROM identity)",
            "provenance_shadow_minus_candidate": f"SELECT {provenance} FROM shadow.valuation_provenance EXCEPT SELECT {provenance} FROM candidate.valuation_provenance",
            "provenance_candidate_minus_shadow": f"SELECT {provenance} FROM candidate.valuation_provenance EXCEPT SELECT {provenance} FROM shadow.valuation_provenance",
            "shadow_provenance_orphans": "SELECT p.code,p.date FROM shadow.valuation_provenance p LEFT JOIN shadow.stock_valuation v ON v.code=p.code AND v.date=p.date WHERE v.code IS NULL",
            "shadow_identity_without_provenance": "SELECT v.code,v.date FROM shadow.stock_valuation v LEFT JOIN shadow.valuation_provenance p ON p.code=v.code AND p.date=v.date WHERE v.code IN (SELECT code FROM identity) AND p.code IS NULL",
            "shadow_provenance_out_of_scope": "SELECT code,date FROM shadow.valuation_provenance WHERE code NOT IN (SELECT code FROM identity)",
            "formal_identity_without_candidate_key": "SELECT f.code,f.date FROM formal.stock_valuation f WHERE f.code IN (SELECT code FROM identity) AND NOT EXISTS (SELECT 1 FROM candidate.stock_valuation c WHERE c.code=f.code AND replace(c.date,'-','')=replace(f.date,'-',''))",
        }
        anomaly_sql.update(queries)
        for name, sql in queries.items():
            value = count(connection, f"SELECT COUNT(*) FROM ({sql})")
            counts[name] = value
            gates.append({"gate": name, "passed": value == 0, "detail": value})
        row_counts = {
            "formal_total": count(connection, "SELECT COUNT(*) FROM formal.stock_valuation"),
            "formal_identity": count(connection, "SELECT COUNT(*) FROM formal.stock_valuation WHERE code IN (SELECT code FROM identity)"),
            "formal_preserved": count(connection, "SELECT COUNT(*) FROM formal.stock_valuation WHERE code NOT IN (SELECT code FROM identity)"),
            "candidate_total": count(connection, "SELECT COUNT(*) FROM candidate.stock_valuation"),
            "shadow_total": count(connection, "SELECT COUNT(*) FROM shadow.stock_valuation"),
            "shadow_identity": count(connection, "SELECT COUNT(*) FROM shadow.stock_valuation WHERE code IN (SELECT code FROM identity)"),
            "shadow_preserved": count(connection, "SELECT COUNT(*) FROM shadow.stock_valuation WHERE code NOT IN (SELECT code FROM identity)"),
            "candidate_provenance": count(connection, "SELECT COUNT(*) FROM candidate.valuation_provenance"),
            "shadow_provenance": count(connection, "SELECT COUNT(*) FROM shadow.valuation_provenance"),
        }
        expected_shadow = row_counts["formal_preserved"] + row_counts["candidate_total"]
        conservation = (
            row_counts["shadow_total"] == expected_shadow
            and row_counts["shadow_identity"] == row_counts["candidate_total"]
            and row_counts["shadow_preserved"] == row_counts["formal_preserved"]
            and row_counts["shadow_provenance"] == row_counts["candidate_provenance"] == row_counts["candidate_total"]
        )
        gates.append({"gate": "three_way_row_conservation", "passed": conservation, "detail": {**row_counts, "expected_shadow": expected_shadow}})
        counts["rows"] = row_counts
        counts["expected_replacement_rows"] = row_counts["candidate_total"] if counts["identity_shadow_minus_candidate"] == counts["identity_candidate_minus_shadow"] == 0 else 0
        counts["formal_preserved_rows"] = row_counts["formal_preserved"] if counts["preserved_shadow_minus_formal"] == counts["preserved_formal_minus_shadow"] == 0 else 0
        failed_queries = {name: sql for name, sql in queries.items() if counts[name]}
        anomalies = {
            name: {
                "count": counts[name],
                "sample": sample(connection, f"SELECT * FROM ({sql}) ORDER BY 1,2", inputs.max_samples),
                "digest": sql_digest(connection, f"SELECT * FROM ({sql}) ORDER BY 1,2", deadline),
            }
            for name, sql in failed_queries.items()
        }
    except sqlite3.OperationalError as exc:
        if "interrupted" in str(exc).lower() and time.monotonic() >= deadline.ends:
            raise AuditTimeoutError("candidate-aware audit timed out during SQLite evaluation") from exc
        raise
    finally:
        connection.close()
    after = {name: db_state(path) for name, path in (("formal", formal), ("shadow", shadow), ("candidate", candidate))}
    integrity = {
        name: integrity_projection(before[name]) == integrity_projection(after[name])
        for name in before
    }
    gates.append({"gate": "input_file_integrity_unchanged", "passed": all(integrity.values()), "detail": integrity})
    passed = all(gate["passed"] for gate in gates)
    return {
        "verdict": "PASS_CANDIDATE_AWARE_SHADOW_EXPECTATION_PROVEN" if passed else "FAIL_CANDIDATE_AWARE_SHADOW_ANOMALY",
        "generated_at": utc_now(),
        "elapsed_ms": deadline.elapsed_ms,
        "inputs": {
            "formal_db": str(formal),
            "shadow_root": str(shadow_root),
            "shadow_db": str(shadow),
            "candidate_root": str(candidate_root),
            "candidate_db": str(candidate),
            "identity": str(identity),
            "outer_seal": outer_seal,
            "inner_seal": inner_seal,
            "expected_sha256": {"formal": expected_formal, "shadow": expected_shadow, "candidate": expected_candidate},
            "formal_sha256_verified": actual_formal_sha,
            "formal_state_before": before["formal"],
            "shadow_state_before": before["shadow"],
            "candidate_state_before": before["candidate"],
        },
        "identity_codes": len(codes),
        "gates": gates,
        "counts": counts,
        "anomalies": anomalies,
        "input_state_after": after,
    }


def run(inputs: Inputs) -> dict[str, Any]:
    root = inputs.evidence_root.resolve()
    if root.exists():
        raise FileExistsError(f"evidence root already exists: {root}")
    root.mkdir(parents=True)
    lock = root / ".audit.lock"
    write_json_no_clobber(lock, {"pid": os.getpid(), "started_at": utc_now()})
    try:
        result = audit(inputs)
        write_json_no_clobber(root / "analysis/candidate-aware-verdict.json", result)
        if not result["verdict"].startswith("PASS_"):
            raise AuditError(result["verdict"])
        lock.unlink()
        return result
    except Exception as exc:
        failure = {
            "status": "FAIL_CANDIDATE_AWARE_AUDIT",
            "failed_at": utc_now(),
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        try:
            write_json_no_clobber(root / "analysis/failure.json", failure)
        except FileExistsError:
            pass
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--formal-db", type=Path, required=True)
    parser.add_argument("--shadow-root", type=Path, required=True)
    parser.add_argument("--shadow-db", type=Path, required=True)
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--candidate-db", type=Path, required=True)
    parser.add_argument("--identity", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--expected-formal-sha256", required=True)
    parser.add_argument("--expected-shadow-sha256", required=True)
    parser.add_argument("--expected-candidate-sha256", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=900.0)
    parser.add_argument("--max-samples", type=int, default=20)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.max_samples <= 0:
        raise ValueError("max_samples must be positive")
    result = run(Inputs(
        formal_db=args.formal_db,
        shadow_root=args.shadow_root,
        shadow_db=args.shadow_db,
        candidate_root=args.candidate_root,
        candidate_db=args.candidate_db,
        identity=args.identity,
        evidence_root=args.evidence_root,
        expected_formal_sha256=args.expected_formal_sha256,
        expected_shadow_sha256=args.expected_shadow_sha256,
        expected_candidate_sha256=args.expected_candidate_sha256,
        timeout_seconds=args.timeout_seconds,
        max_samples=args.max_samples,
    ))
    print(json.dumps({
        "verdict": result["verdict"],
        "elapsed_ms": result["elapsed_ms"],
        "identity_codes": result["identity_codes"],
        "counts": result["counts"],
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
