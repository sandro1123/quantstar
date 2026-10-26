#!/usr/bin/env python3
"""Build a fail-closed QuantStar shadow DB from formal qs.db and a sealed BJ candidate.

This tool never writes the formal database. It creates a SQLite API snapshot in a new
isolated evidence root, classifies normalized-date conflicts, applies the sealed BJ
candidate to the snapshot, copies row provenance, and publishes the shadow DB with a
no-clobber hard-link operation only after all invariants pass.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

VALUATION_COLUMNS = (
    "code",
    "date",
    "pe_ttm",
    "pb_mrq",
    "ps_ttm",
    "market_cap",
    "total_shares",
    "dividend_yield",
)
PROVENANCE_COLUMNS = (
    "code",
    "date",
    "request_id",
    "source",
    "source_column_map",
    "raw_row_sha256",
    "normalized_record_sha256",
    "retrieved_at",
    "adapter_sha256",
    "identity_snapshot_sha256",
    "mapping_snapshot_sha256",
    "normalization_version",
)
FORMAL_VALUATION_SCHEMA = (
    (0, "code", "TEXT", 1, None, 1),
    (1, "date", "TEXT", 1, None, 2),
    (2, "pe_ttm", "REAL", 0, "0", 0),
    (3, "pb_mrq", "REAL", 0, "0", 0),
    (4, "ps_ttm", "REAL", 0, "0", 0),
    (5, "market_cap", "REAL", 0, "0", 0),
    (6, "total_shares", "REAL", 0, "0", 0),
    (7, "dividend_yield", "REAL", 0, None, 0),
)
CANDIDATE_VALUATION_SCHEMA = (
    (0, "code", "TEXT", 1, None, 1),
    (1, "date", "TEXT", 1, None, 2),
    (2, "pe_ttm", "REAL", 0, None, 0),
    (3, "pb_mrq", "REAL", 0, None, 0),
    (4, "ps_ttm", "REAL", 0, None, 0),
    (5, "market_cap", "REAL", 0, None, 0),
    (6, "total_shares", "REAL", 0, None, 0),
    (7, "dividend_yield", "REAL", 0, None, 0),
)
PROVENANCE_SCHEMA = (
    (0, "code", "TEXT", 1, None, 1),
    (1, "date", "TEXT", 1, None, 2),
    (2, "request_id", "TEXT", 1, None, 0),
    (3, "source", "TEXT", 1, None, 0),
    (4, "source_column_map", "TEXT", 0, None, 0),
    (5, "raw_row_sha256", "TEXT", 0, None, 0),
    (6, "normalized_record_sha256", "TEXT", 0, None, 0),
    (7, "retrieved_at", "TEXT", 0, None, 0),
    (8, "adapter_sha256", "TEXT", 0, None, 0),
    (9, "identity_snapshot_sha256", "TEXT", 0, None, 0),
    (10, "mapping_snapshot_sha256", "TEXT", 0, None, 0),
    (11, "normalization_version", "TEXT", 0, None, 0),
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json_no_clobber(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def publish_no_clobber(source: Path, target: Path) -> None:
    if target.exists():
        raise FileExistsError(f"shadow target already exists: {target}")
    os.link(source, target)
    source.unlink()


def file_state(path: Path, include_sha: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"path": str(path), "exists": path.exists()}
    if path.exists():
        stat = path.stat()
        result.update(size=stat.st_size, mtime_ns=stat.st_mtime_ns, inode=stat.st_ino)
        if include_sha:
            result["sha256"] = sha256_file(path)
    return result


def formal_state(db: Path, include_sha: bool = True) -> dict[str, Any]:
    return {
        "db": file_state(db, include_sha),
        "wal": file_state(Path(str(db) + "-wal"), include_sha),
        "shm": file_state(Path(str(db) + "-shm"), include_sha),
    }


def formal_integrity_state(state: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for label, item in state.items():
        checked = dict(item)
        if label in ("wal", "shm"):
            checked.pop("mtime_ns", None)
        result[label] = checked
    return result


def require_formal_integrity_unchanged(
    before: dict[str, Any],
    after: dict[str, Any],
    phase: str,
) -> None:
    if formal_integrity_state(after) != formal_integrity_state(before):
        raise RuntimeError(f"formal DB/WAL/SHM integrity changed {phase}")


def connect_ro(path: Path, immutable: bool = True) -> sqlite3.Connection:
    suffix = "?mode=ro&immutable=1" if immutable else "?mode=ro"
    conn = sqlite3.connect(f"file:{path.as_posix()}{suffix}", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def table_schema(conn: sqlite3.Connection, table: str) -> tuple[tuple[Any, ...], ...]:
    return tuple(tuple(row) for row in conn.execute(f"PRAGMA table_info({table})"))


def require_exact_schema(
    conn: sqlite3.Connection,
    table: str,
    expected: Iterable[tuple[Any, ...]],
) -> None:
    actual = table_schema(conn, table)
    expected_tuple = tuple(expected)
    if actual != expected_tuple:
        raise ValueError(f"{table} schema differs: expected={expected_tuple!r} actual={actual!r}")


def sqlite_master_hash(conn: sqlite3.Connection, excluded_names: Iterable[str] = ()) -> str:
    excluded = set(excluded_names)
    digest = hashlib.sha256()
    count = 0
    rows = conn.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name,tbl_name"
    )
    for row in rows:
        if row[1] in excluded or row[2] in excluded:
            continue
        digest.update(json.dumps(list(row), separators=(",", ":"), ensure_ascii=False).encode())
        digest.update(b"\n")
        count += 1
    return f"{count}:{digest.hexdigest()}"


def verify_sealed_candidate_root(root: Path, candidate_db: Path) -> dict[str, Any]:
    root = root.resolve()
    candidate_db = candidate_db.resolve()
    try:
        candidate_rel = candidate_db.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError("candidate DB must be inside candidate evidence root") from exc
    final_path = root / "manifests/final.sha256"
    manifest_path = root / "manifests/manifest.json"
    if not final_path.is_file() or not manifest_path.is_file():
        raise ValueError("candidate evidence root is not finally sealed")
    entries: list[tuple[str, str]] = []
    for line_number, raw in enumerate(final_path.read_text(encoding="utf-8").splitlines(), 1):
        parts = raw.split(None, 1)
        if len(parts) != 2 or len(parts[0]) != 64:
            raise ValueError(f"invalid final.sha256 line {line_number}")
        digest, rel = parts[0].lower(), parts[1].strip().lstrip("*")
        rel_path = Path(rel)
        if (
            not all(ch in "0123456789abcdef" for ch in digest)
            or rel_path.is_absolute()
            or ".." in rel_path.parts
            or rel in ("", "manifests/final.sha256")
        ):
            raise ValueError(f"unsafe final.sha256 entry: {rel!r}")
        entries.append((digest, rel_path.as_posix()))
    paths = [rel for _, rel in entries]
    if len(paths) != len(set(paths)):
        raise ValueError("duplicate paths in final.sha256")
    all_paths = list(root.rglob("*"))
    symlinks = [path.relative_to(root).as_posix() for path in all_paths if path.is_symlink()]
    if symlinks:
        raise ValueError(f"sealed candidate contains symlinks: {symlinks}")
    seal_locks = [
        path.relative_to(root).as_posix()
        for path in all_paths
        if path.is_file() and path.name == ".seal.lock"
    ]
    if seal_locks:
        raise ValueError(f"sealed candidate still has seal lock: {seal_locks}")
    actual_files = {
        path.relative_to(root).as_posix()
        for path in all_paths
        if path.is_file() and path != final_path and path.name != ".seal.lock"
    }
    listed = set(paths)
    if actual_files != listed:
        raise ValueError(
            f"sealed candidate file set mismatch: missing={sorted(listed-actual_files)} "
            f"unmanifested={sorted(actual_files-listed)}"
        )
    mismatches = []
    for expected, rel in entries:
        actual = sha256_file(root / rel)
        if actual != expected:
            mismatches.append(rel)
    if mismatches:
        raise ValueError(f"sealed candidate SHA mismatch: {mismatches}")
    if paths.count("manifests/manifest.json") != 1 or paths.count(candidate_rel) != 1:
        raise ValueError("final.sha256 must bind manifest.json and candidate DB exactly once")
    return {
        "root": str(root),
        "candidate_relative_path": candidate_rel,
        "payload_files": len(entries),
        "manifest_sha256": sha256_file(manifest_path),
        "final_sha256_file_sha256": sha256_file(final_path),
        "candidate_sha256": sha256_file(candidate_db),
    }


def materialize_sealed_candidate(
    source_root: Path,
    source_candidate_db: Path,
    source_identity: Path,
    destination_root: Path,
) -> tuple[Path, Path, Path]:
    source_root = source_root.resolve()
    source_candidate_db = source_candidate_db.resolve()
    source_identity = source_identity.resolve()
    try:
        candidate_rel = source_candidate_db.relative_to(source_root)
        identity_rel = source_identity.relative_to(source_root)
    except ValueError as exc:
        raise ValueError("candidate DB and identity must both be inside sealed candidate root") from exc
    if destination_root.exists():
        raise FileExistsError(f"materialized candidate root already exists: {destination_root}")
    shutil.copytree(source_root, destination_root, copy_function=shutil.copy2)
    return destination_root, destination_root / candidate_rel, destination_root / identity_rel


def load_identity_codes(identity_path: Path) -> list[str]:
    payload = json.loads(identity_path.read_text(encoding="utf-8"))
    securities = payload.get("securities") if isinstance(payload, dict) else None
    if not isinstance(securities, list):
        raise ValueError("identity securities must be a list")
    codes: list[str] = []
    for item in securities:
        code = item.get("code") if isinstance(item, dict) else None
        if not isinstance(code, str) or len(code) != 6 or not code.isdigit() or not code.startswith("92"):
            raise ValueError(f"invalid BJ identity code: {code!r}")
        codes.append(code)
    if not codes or len(codes) != len(set(codes)):
        raise ValueError("identity codes must be non-empty and unique")
    return sorted(codes)


def validate_candidate(candidate_db: Path, identity_codes: list[str]) -> dict[str, Any]:
    with connect_ro(candidate_db) as conn:
        require_exact_schema(conn, "stock_valuation", CANDIDATE_VALUATION_SCHEMA)
        require_exact_schema(conn, "valuation_provenance", PROVENANCE_SCHEMA)
        quick = conn.execute("PRAGMA quick_check").fetchone()[0]
        if quick != "ok":
            raise ValueError(f"candidate quick_check failed: {quick}")
        codes = [row[0] for row in conn.execute("SELECT DISTINCT code FROM stock_valuation ORDER BY code")]
        if codes != identity_codes:
            raise ValueError("candidate code set differs from sealed identity")
        rows = conn.execute("SELECT COUNT(*) FROM stock_valuation").fetchone()[0]
        provenance = conn.execute("SELECT COUNT(*) FROM valuation_provenance").fetchone()[0]
        missing = conn.execute(
            "SELECT COUNT(*) FROM stock_valuation v LEFT JOIN valuation_provenance p "
            "ON p.code=v.code AND p.date=v.date WHERE p.code IS NULL"
        ).fetchone()[0]
        duplicate_keys = conn.execute(
            "SELECT COUNT(*) FROM (SELECT code,date,COUNT(*) c FROM stock_valuation "
            "GROUP BY code,date HAVING c>1)"
        ).fetchone()[0]
        invalid_date_values = []
        for (value,) in conn.execute("SELECT DISTINCT date FROM stock_valuation ORDER BY date"):
            try:
                if datetime.strptime(value, "%Y-%m-%d").date().isoformat() != value:
                    invalid_date_values.append(value)
            except (TypeError, ValueError):
                invalid_date_values.append(value)
        invalid_dates = len(invalid_date_values)
        invalid_normalized_keys = conn.execute(
            "SELECT COUNT(*) FROM (SELECT code,replace(date,'-','') normalized_date,COUNT(*) c "
            "FROM stock_valuation GROUP BY code,normalized_date HAVING c>1)"
        ).fetchone()[0]
        value_columns = (
            "pe_ttm",
            "pb_mrq",
            "ps_ttm",
            "market_cap",
            "total_shares",
            "dividend_yield",
        )
        nonfinite_values = 0
        for row in conn.execute(f"SELECT {','.join(value_columns)} FROM stock_valuation"):
            nonfinite_values += sum(
                value is not None and not math.isfinite(float(value))
                for value in row
            )
        if (
            rows != provenance
            or missing
            or duplicate_keys
            or invalid_dates
            or invalid_normalized_keys
            or nonfinite_values
        ):
            raise ValueError(
                "candidate invariants failed: "
                f"rows={rows} provenance={provenance} missing={missing} "
                f"duplicates={duplicate_keys} invalid_dates={invalid_dates} "
                f"normalized_duplicates={invalid_normalized_keys} "
                f"nonfinite_values={nonfinite_values}"
            )
        return {
            "quick_check": quick,
            "rows": rows,
            "provenance_rows": provenance,
            "codes": len(codes),
            "min_date": conn.execute("SELECT MIN(date) FROM stock_valuation").fetchone()[0],
            "max_date": conn.execute("SELECT MAX(date) FROM stock_valuation").fetchone()[0],
            "sha256": sha256_file(candidate_db),
        }


def normalized_date_sql(column: str = "date") -> str:
    return f"replace({column}, '-', '')"


def row_stream_hash(conn: sqlite3.Connection, identity_codes: list[str], exclude: bool) -> str:
    digest = hashlib.sha256()
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS nova_identity(code TEXT PRIMARY KEY)")
    conn.execute("DELETE FROM nova_identity")
    conn.executemany("INSERT INTO nova_identity(code) VALUES (?)", ((code,) for code in identity_codes))
    op = "NOT IN" if exclude else "IN"
    sql = (
        "SELECT code,date,pe_ttm,pb_mrq,ps_ttm,market_cap,total_shares,dividend_yield "
        f"FROM stock_valuation WHERE code {op} (SELECT code FROM nova_identity) ORDER BY code,date"
    )
    count = 0
    for row in conn.execute(sql):
        digest.update(json.dumps(list(row), separators=(",", ":"), ensure_ascii=False).encode())
        digest.update(b"\n")
        count += 1
    return f"{count}:{digest.hexdigest()}"


def create_consistent_snapshot(formal_db: Path, partial_db: Path) -> None:
    partial_db.parent.mkdir(parents=True, exist_ok=True)
    if partial_db.exists():
        raise FileExistsError(f"partial shadow already exists: {partial_db}")
    source = connect_ro(formal_db, immutable=False)
    try:
        target = sqlite3.connect(partial_db)
        try:
            source.backup(target, pages=-1)
        finally:
            target.close()
    finally:
        source.close()
    os.chmod(partial_db, 0o600)


def validate_formal_identity_dates(
    shadow: sqlite3.Connection,
    identity_codes: list[str],
) -> dict[str, int]:
    shadow.execute("CREATE TEMP TABLE IF NOT EXISTS nova_identity(code TEXT PRIMARY KEY)")
    shadow.execute("DELETE FROM nova_identity")
    shadow.executemany("INSERT INTO nova_identity(code) VALUES (?)", ((code,) for code in identity_codes))
    dates: list[tuple[str, str]] = list(
        shadow.execute(
            "SELECT code,date FROM stock_valuation "
            "WHERE code IN (SELECT code FROM nova_identity) ORDER BY code,date"
        )
    )
    invalid: list[tuple[str, str]] = []
    normalized: list[tuple[str, str]] = []
    for code, value in dates:
        try:
            if not isinstance(value, str):
                raise ValueError("not a string")
            fmt = "%Y-%m-%d" if "-" in value else "%Y%m%d"
            normalized_value = datetime.strptime(value, fmt).date().isoformat()
            if datetime.strptime(normalized_value, "%Y-%m-%d").date().isoformat() != normalized_value:
                raise ValueError("non-canonical normalized date")
            normalized.append((code, normalized_value))
        except (TypeError, ValueError):
            invalid.append((code, value))
    duplicates = len(normalized) - len(set(normalized))
    if invalid or duplicates:
        raise ValueError(
            f"formal BJ date invariants failed: invalid={invalid[:10]} normalized_duplicates={duplicates}"
        )
    return {
        "formal_identity_date_rows": len(dates),
        "formal_identity_invalid_dates": len(invalid),
        "formal_identity_normalized_duplicates": duplicates,
    }


def classify_conflicts(shadow: sqlite3.Connection, candidate_db: Path, identity_codes: list[str]) -> dict[str, Any]:
    shadow.execute("CREATE TEMP TABLE IF NOT EXISTS nova_identity(code TEXT PRIMARY KEY)")
    shadow.execute("DELETE FROM nova_identity")
    shadow.executemany("INSERT INTO nova_identity(code) VALUES (?)", ((code,) for code in identity_codes))
    shadow.commit()
    shadow.execute("ATTACH DATABASE ? AS candidate", (candidate_db.as_posix(),))
    try:
        normalized_overlap = shadow.execute(
            "SELECT COUNT(*) FROM main.stock_valuation f JOIN candidate.stock_valuation c "
            "ON f.code=c.code AND replace(f.date,'-','')=replace(c.date,'-','') "
            "WHERE f.code IN (SELECT code FROM nova_identity)"
        ).fetchone()[0]
        formal_identity_rows = shadow.execute(
            "SELECT COUNT(*) FROM main.stock_valuation WHERE code IN (SELECT code FROM nova_identity)"
        ).fetchone()[0]
        ambiguous_formal_normalized_keys = shadow.execute(
            "SELECT COUNT(*) FROM (SELECT code,replace(date,'-','') normalized_date,COUNT(*) c "
            "FROM main.stock_valuation WHERE code IN (SELECT code FROM nova_identity) "
            "GROUP BY code,normalized_date HAVING c>1)"
        ).fetchone()[0]
        if ambiguous_formal_normalized_keys:
            raise ValueError(
                f"formal BJ normalized-date keys are ambiguous: {ambiguous_formal_normalized_keys}"
            )
        candidate_rows = shadow.execute("SELECT COUNT(*) FROM candidate.stock_valuation").fetchone()[0]
        conflicts = [
            dict(row)
            for row in shadow.execute(
                "SELECT f.code, f.date formal_date, c.date candidate_date, "
                "f.pe_ttm formal_pe_ttm, c.pe_ttm candidate_pe_ttm, "
                "f.pb_mrq formal_pb_mrq, c.pb_mrq candidate_pb_mrq, "
                "f.ps_ttm formal_ps_ttm, c.ps_ttm candidate_ps_ttm, "
                "f.market_cap formal_market_cap, c.market_cap candidate_market_cap, "
                "f.total_shares formal_total_shares, c.total_shares candidate_total_shares, "
                "f.dividend_yield formal_dividend_yield, c.dividend_yield candidate_dividend_yield "
                "FROM main.stock_valuation f JOIN candidate.stock_valuation c "
                "ON f.code=c.code AND replace(f.date,'-','')=replace(c.date,'-','') "
                "WHERE f.code IN (SELECT code FROM nova_identity) ORDER BY f.code,f.date"
            )
        ]
        value_columns = (
            "pe_ttm",
            "pb_mrq",
            "ps_ttm",
            "market_cap",
            "total_shares",
            "dividend_yield",
        )
        value_conflicts = 0
        for record in conflicts:
            differing = [
                column
                for column in value_columns
                if record[f"formal_{column}"] != record[f"candidate_{column}"]
            ]
            record["differing_columns"] = differing
            value_conflicts += bool(differing)
        return {
            "formal_identity_rows": formal_identity_rows,
            "candidate_rows": candidate_rows,
            "normalized_overlap_rows": normalized_overlap,
            "exact_normalized_overlap_rows": normalized_overlap - value_conflicts,
            "value_conflict_rows": value_conflicts,
            "formal_identity_rows_without_candidate_overlap": formal_identity_rows - normalized_overlap,
            "candidate_wins_rows": normalized_overlap,
            "conflicts": conflicts,
        }
    finally:
        shadow.execute("DETACH DATABASE candidate")


def merge_candidate(shadow: sqlite3.Connection, candidate_db: Path, identity_codes: list[str]) -> dict[str, Any]:
    before_total = shadow.execute("SELECT COUNT(*) FROM stock_valuation").fetchone()[0]
    non_identity_before = row_stream_hash(shadow, identity_codes, exclude=True)
    formal_date_validation = validate_formal_identity_dates(shadow, identity_codes)
    conflict = classify_conflicts(shadow, candidate_db, identity_codes)
    conflict.update(formal_date_validation)
    shadow.execute("ATTACH DATABASE ? AS candidate", (candidate_db.as_posix(),))
    columns = ",".join(VALUATION_COLUMNS)
    provenance = ",".join(PROVENANCE_COLUMNS)
    try:
        shadow.execute("BEGIN IMMEDIATE")
        deleted = shadow.execute(
            "DELETE FROM main.stock_valuation WHERE code IN (SELECT code FROM nova_identity) "
            "AND EXISTS (SELECT 1 FROM candidate.stock_valuation c WHERE c.code=main.stock_valuation.code "
            "AND replace(c.date,'-','')=replace(main.stock_valuation.date,'-',''))"
        ).rowcount
        shadow.execute(f"INSERT INTO main.stock_valuation({columns}) SELECT {columns} FROM candidate.stock_valuation")
        shadow.execute(
            "CREATE TABLE main.valuation_provenance ("
            "code TEXT NOT NULL, date TEXT NOT NULL, request_id TEXT NOT NULL, source TEXT NOT NULL, "
            "source_column_map TEXT, raw_row_sha256 TEXT, normalized_record_sha256 TEXT, retrieved_at TEXT, "
            "adapter_sha256 TEXT, identity_snapshot_sha256 TEXT, mapping_snapshot_sha256 TEXT, "
            "normalization_version TEXT, PRIMARY KEY(code,date))"
        )
        shadow.execute(
            f"INSERT INTO main.valuation_provenance({provenance}) "
            f"SELECT {provenance} FROM candidate.valuation_provenance"
        )
        shadow.commit()
    except Exception:
        shadow.rollback()
        raise
    finally:
        shadow.execute("DETACH DATABASE candidate")
    after_total = shadow.execute("SELECT COUNT(*) FROM stock_valuation").fetchone()[0]
    candidate_rows = conflict["candidate_rows"]
    expected_total = before_total - deleted + candidate_rows
    if after_total != expected_total or deleted != conflict["normalized_overlap_rows"]:
        raise ValueError(
            f"merge conservation failed: before={before_total} deleted={deleted} "
            f"candidate={candidate_rows} after={after_total} expected={expected_total}"
        )
    provenance_rows = shadow.execute("SELECT COUNT(*) FROM valuation_provenance").fetchone()[0]
    if provenance_rows != candidate_rows:
        raise ValueError(f"provenance count mismatch: {provenance_rows} != {candidate_rows}")
    non_identity_after = row_stream_hash(shadow, identity_codes, exclude=True)
    if non_identity_after != non_identity_before:
        raise ValueError("non-BJ stock_valuation rows changed")
    quick = shadow.execute("PRAGMA quick_check").fetchone()[0]
    if quick != "ok":
        raise ValueError(f"shadow quick_check failed: {quick}")
    conflict.update(
        before_total=before_total,
        deleted_rows=deleted,
        after_total=after_total,
        expected_total=expected_total,
        provenance_rows=provenance_rows,
        non_identity_hash_before=non_identity_before,
        non_identity_hash_after=non_identity_after,
        quick_check=quick,
    )
    return conflict


def git_state(repo: Path) -> dict[str, Any]:
    return {
        "branch": subprocess.check_output(["git", "-C", str(repo), "branch", "--show-current"], text=True).strip(),
        "head": subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip(),
        "status_count": len(subprocess.check_output(["git", "-C", str(repo), "status", "--short"], text=True).splitlines()),
    }


def run(
    formal_db: Path,
    candidate_db: Path,
    candidate_evidence_root: Path,
    identity: Path,
    evidence_root: Path,
    repo: Path,
) -> dict[str, Any]:
    if evidence_root.exists():
        raise FileExistsError(f"evidence root already exists: {evidence_root}")
    evidence_root.mkdir(parents=True)
    lock = evidence_root / ".build.lock"
    lock.write_text(f"pid={os.getpid()} started_at={utc_now()}\n", encoding="utf-8")
    partial = evidence_root / "shadow/qs-shadow.db.partial"
    published = evidence_root / "shadow/qs-shadow.db"
    try:
        source_seal = verify_sealed_candidate_root(candidate_evidence_root, candidate_db)
        materialized_root, materialized_candidate, materialized_identity = materialize_sealed_candidate(
            candidate_evidence_root,
            candidate_db,
            identity,
            evidence_root / "inputs/sealed-candidate",
        )
        sealed_candidate = verify_sealed_candidate_root(materialized_root, materialized_candidate)
        if sealed_candidate["final_sha256_file_sha256"] != source_seal["final_sha256_file_sha256"]:
            raise ValueError("materialized candidate final.sha256 differs from source")
        if sha256_file(materialized_identity) != sha256_file(identity):
            raise ValueError("materialized candidate identity differs from source")
        candidate_db = materialized_candidate
        identity = materialized_identity
        codes = load_identity_codes(identity)
        candidate_summary = validate_candidate(candidate_db, codes)
        if candidate_summary["sha256"] != sealed_candidate["candidate_sha256"]:
            raise ValueError("candidate DB SHA differs from sealed binding")
        before = formal_state(formal_db, include_sha=True)
        baseline = {
            "recorded_at": utc_now(),
            "formal": before,
            "sealed_candidate": sealed_candidate,
            "candidate": candidate_summary,
            "builder_sha256": sha256_file(Path(__file__).resolve()),
            "identity_path": str(identity),
            "identity_sha256": sha256_file(identity),
            "identity_codes": len(codes),
            "repo": git_state(repo),
        }
        write_json_no_clobber(evidence_root / "baseline/g0.json", baseline)
        create_consistent_snapshot(formal_db, partial)
        after_snapshot = formal_state(formal_db, include_sha=True)
        require_formal_integrity_unchanged(before, after_snapshot, "while creating shadow snapshot")
        snapshot_sha_before_merge = sha256_file(partial)
        shadow = sqlite3.connect(partial)
        shadow.row_factory = sqlite3.Row
        try:
            require_exact_schema(shadow, "stock_valuation", FORMAL_VALUATION_SCHEMA)
            if shadow.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='valuation_provenance'"
            ).fetchone()[0]:
                raise ValueError("formal snapshot unexpectedly already has valuation_provenance")
            master_hash_before = sqlite_master_hash(shadow)
            merge_report = merge_candidate(shadow, candidate_db, codes)
            require_exact_schema(shadow, "stock_valuation", FORMAL_VALUATION_SCHEMA)
            require_exact_schema(shadow, "valuation_provenance", PROVENANCE_SCHEMA)
            master_hash_after_excluding_provenance = sqlite_master_hash(
                shadow,
                excluded_names=("valuation_provenance",),
            )
            if master_hash_after_excluding_provenance != master_hash_before:
                raise ValueError("shadow sqlite_master changed beyond valuation_provenance addition")
            merge_report.update(
                sqlite_master_hash_before=master_hash_before,
                sqlite_master_hash_after_excluding_provenance=master_hash_after_excluding_provenance,
            )
        finally:
            shadow.close()
        final_formal = formal_state(formal_db, include_sha=True)
        require_formal_integrity_unchanged(before, final_formal, "during shadow merge")
        merge_report.update(
            generated_at=utc_now(),
            formal_snapshot_sha256_before_merge=snapshot_sha_before_merge,
            shadow_sha256=sha256_file(partial),
            formal_integrity_unchanged=True,
            formal_sidecar_mtime_observation={
                "before": {
                    name: state.get("mtime_ns")
                    for name, state in before.items()
                    if name in ("wal", "shm")
                },
                "after_snapshot": {
                    name: state.get("mtime_ns")
                    for name, state in after_snapshot.items()
                    if name in ("wal", "shm")
                },
                "after_merge": {
                    name: state.get("mtime_ns")
                    for name, state in final_formal.items()
                    if name in ("wal", "shm")
                },
            },
            identity_codes=len(codes),
            candidate_summary=candidate_summary,
        )
        write_json_no_clobber(evidence_root / "analysis/merge-report.json", merge_report)
        ready = {
            "status": "READY_TO_PUBLISH_SHADOW",
            "evidence_root": str(evidence_root),
            "shadow_db": str(published),
            "shadow_sha256": merge_report["shadow_sha256"],
            "rows": merge_report["after_total"],
            "provenance_rows": merge_report["provenance_rows"],
            "deleted_conflicts": merge_report["deleted_rows"],
        }
        write_json_no_clobber(evidence_root / "analysis/pre-publish.json", ready)
        publish_no_clobber(partial, published)
        if sha256_file(published) != merge_report["shadow_sha256"]:
            raise RuntimeError("published shadow SHA mismatch")
        result = {**ready, "status": "PASS_SHADOW_MERGE_BUILT"}
        write_json_no_clobber(evidence_root / "analysis/build-result.json", result)
        lock.unlink()
        return result
    except Exception as exc:
        # Keep lock and a forensic partial DB; never leave a final-looking shadow on failure.
        if published.exists() and not partial.exists():
            try:
                os.link(published, partial)
                published.unlink()
            except OSError:
                pass
        failure = {
            "status": "SHADOW_MERGE_FAILED",
            "failed_at": utc_now(),
            "exception_type": type(exc).__name__,
            "message": str(exc),
            "formal_db": str(formal_db),
            "candidate_evidence_root": str(candidate_evidence_root),
            "evidence_root": str(evidence_root),
            "partial_exists": partial.exists(),
            "published_exists": published.exists(),
            "lock_retained": lock.exists(),
        }
        try:
            write_json_no_clobber(evidence_root / "analysis/failure.json", failure)
        except Exception:
            pass
        raise


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--formal-db", type=Path, required=True)
    parser.add_argument("--candidate-db", type=Path, required=True)
    parser.add_argument("--candidate-evidence-root", type=Path, required=True)
    parser.add_argument("--identity", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = run(
            args.formal_db,
            args.candidate_db,
            args.candidate_evidence_root,
            args.identity,
            args.evidence_root,
            args.repo,
        )
    except Exception as exc:
        print(f"SHADOW_MERGE_FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
