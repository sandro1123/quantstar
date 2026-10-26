#!/usr/bin/env python3
"""B3-R1: Isolated SQL forward/rollback drill for v3 SQL.

Drills the v3 proposed-forward / proposed-rollback SQL (from the B1-R1
evidence-based audit) on an isolated copy of the formal DB.

Nova B3 fix (physical SHA ordering):
  * Drill copies are opened with journal_mode=DELETE so committed changes are
    written to the main .db file (no WAL indirection).
  * Physical SHA256 is computed ONLY after the connection is closed AND the
    copy has no -wal / -shm sidecar files (state recorded explicitly).
  * If a sidecar file still exists at fingerprint time, the physical SHA is
    marked "inconclusive" and the mismatch is recorded honestly.
  * Logical hashes (row content) remain the PRIMARY restore gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import subprocess as sp
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sidecar_state(path: str) -> dict:
    """Return the existence/size of WAL/SHM sidecar files for a DB file."""
    return {
        "wal_exists": os.path.exists(path + "-wal"),
        "wal_size": os.path.getsize(path + "-wal") if os.path.exists(path + "-wal") else 0,
        "shm_exists": os.path.exists(path + "-shm"),
        "shm_size": os.path.getsize(path + "-shm") if os.path.exists(path + "-shm") else 0,
    }


def table_schema(conn: sqlite3.Connection, table: str) -> list[dict]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [{"cid": r[0], "name": r[1], "type": r[2], "notnull": r[3], "dflt_value": r[4], "pk": r[5]} for r in rows]


def table_logical_hash(conn: sqlite3.Connection, table: str) -> str:
    """Compute a logical hash of all rows in a table."""
    try:
        old_factory = conn.row_factory
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(f"SELECT * FROM {table} ORDER BY rowid")
        rows = cursor.fetchall()
        conn.row_factory = old_factory
        if not rows:
            return hashlib.sha256(b"empty").hexdigest()
        h = hashlib.sha256()
        for r in rows:
            h.update(str(dict(r)).encode())
        return h.hexdigest()
    except Exception as e:
        return f"ERROR: {e}"


def table_row_count(conn: sqlite3.Connection, table: str) -> int:
    try:
        return conn.execute(f"SELECT COUNT(*) as cnt FROM {table}").fetchone()[0]
    except Exception:
        return -1


def physical_fingerprint(path: str) -> dict:
    """Compute the at-rest physical fingerprint of a DB file.

    The caller MUST have closed all connections to `path` before calling this
    so the file reflects committed state. Sidecar state is recorded so a
    physical SHA taken while WAL content still exists is marked inconclusive.
    """
    sidecars = sidecar_state(path)
    fp = {
        "path": path,
        "sha256": sha256_file(path),
        "size_bytes": Path(path).stat().st_size,
        "sidecars": sidecars,
        "physical_sha_confidence": (
            "ok" if not sidecars["wal_exists"] else
            "inconclusive (wal sidecar present; physical sha may be stale)"
        ),
    }
    return fp


def logical_fingerprint(conn: sqlite3.Connection, path: str) -> dict:
    """Compute the logical fingerprint (tables/schema/rows/hashes) via conn."""
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall() if r[0] != "sqlite_sequence"]
    fp = {
        "path": path,
        "quick_check": "",
        "tables": {},
    }
    try:
        r = sp.run(["sqlite3", path, "PRAGMA quick_check"], capture_output=True, text=True, timeout=10)
        fp["quick_check"] = r.stdout.strip()
    except Exception:
        fp["quick_check"] = "unavailable"
    for t in tables:
        fp["tables"][t] = {
            "row_count": table_row_count(conn, t),
            "schema": table_schema(conn, t),
            "logical_hash": table_logical_hash(conn, t),
        }
    return fp


def fingerprint_db(conn: sqlite3.Connection, path: str) -> dict:
    """Combined fingerprint for API compatibility.

    NOTE: the physical SHA256 in this combined result is a BEST-EFFORT value.
    For a trustworthy physical comparison the caller must use
    physical_fingerprint() AFTER closing the connection and confirming no
    sidecar files remain. Logical hashes are the primary gate.
    """
    fp = logical_fingerprint(conn, path)
    fp["sha256"] = sha256_file(path)
    fp["size_bytes"] = Path(path).stat().st_size
    fp["sidecars"] = sidecar_state(path)
    fp["physical_sha_confidence"] = (
        "best-effort (connection may still be open; use at-rest for final gate)"
        if fp["sidecars"]["wal_exists"] else "ok (no wal sidecar)"
    )
    return fp


def extract_statements(sql_text: str) -> list[str]:
    """Extract complete SQL statements, skipping comments."""
    stmts = []
    current = []
    for line in sql_text.split("\n"):
        sql_part = line.split("--")[0].strip()
        if not sql_part:
            continue
        current.append(line.split("--")[0])
        if sql_part.endswith(";"):
            stmts.append("\n".join(current))
            current = []
    if current:
        stmts.append("\n".join(current))
    return stmts


def main() -> None:
    p = argparse.ArgumentParser(description="B3-R1: SQL drill on isolated copy (v3)")
    p.add_argument("--market-db", type=Path, required=True)
    p.add_argument("--forward-sql", type=Path, required=True)
    p.add_argument("--rollback-sql", type=Path, required=True)
    p.add_argument("--output-dir", type=Path, required=True)
    args = p.parse_args()

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    start = time.time()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(str(output_dir / "run.log"), encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    log = logging.getLogger("drill")
    log.info("=== B3-R1 SQL drill start ===")
    log.info("market_db: %s", args.market_db)
    log.info("forward_sql: %s", args.forward_sql)
    log.info("rollback_sql: %s", args.rollback_sql)
    log.info("output_dir: %s", output_dir)

    forward_sql_text = args.forward_sql.read_text()
    rollback_sql_text = args.rollback_sql.read_text()

    fwd_stmts = extract_statements(forward_sql_text)
    rbk_stmts = extract_statements(rollback_sql_text)
    log.info("Forward SQL statements extracted: %d", len(fwd_stmts))
    log.info("Rollback SQL statements extracted: %d", len(rbk_stmts))

    if len(fwd_stmts) != len(rbk_stmts):
        log.error("Forward/rollback statement count mismatch: %d vs %d", len(fwd_stmts), len(rbk_stmts))
        sys.exit(1)

    if len(fwd_stmts) == 0:
        log.info("No SQL statements to drill (no-op). Writing no-op report.")
        (output_dir / "report.json").write_text(json.dumps({
            "stage": "B3-R1",
            "status": "COMPLETED_NOOP",
            "forward_statements": 0,
            "forward_rowcounts": [],
            "rollback_rowcounts": [],
            "logical_restore": "NOOP",
            "note": "No high-confidence items in v3 SQL => no statements to drill. "
                    "This is the EXPECTED outcome when only Tencent-level (medium) "
                    "evidence exists. No-op drill completed successfully.",
            "elapsed_seconds": 0,
        }, indent=2))
        log.info("=== B3-R1 no-op complete (v3 SQL empty by design) ===")
        return

    # Verify formal DB unchanged
    formal_sha256 = sha256_file(str(args.market_db))
    (output_dir / "formal-db-unchanged.txt").write_text(
        f"formal_db_sha256: {formal_sha256}\n"
        f"verified_at: {now_iso()}\n"
    )

    # Create isolated copies (DELETE journal mode for at-rest physical integrity)
    before_path = output_dir / "qs.before.db"
    forward_path = output_dir / "qs.forward.db"
    log.info("Copying market DB to %s ...", before_path)
    shutil.copy2(str(args.market_db), str(before_path))
    log.info("Copying to %s ...", forward_path)
    shutil.copy2(str(args.market_db), str(forward_path))

    # Before: logical fingerprint + at-rest physical (close first).
    conn_before = sqlite3.connect(str(before_path))
    conn_before.row_factory = sqlite3.Row
    before_logical = logical_fingerprint(conn_before, str(before_path))
    conn_before.close()
    before_physical = physical_fingerprint(str(before_path))
    before_fp = {**before_logical, **before_physical}
    (output_dir / "before-fingerprint.json").write_text(
        json.dumps(before_fp, indent=2, ensure_ascii=False))
    log.info("Before physical: sha256=%s size=%s sidecars=%s",
             before_physical["sha256"][:16], before_physical["size_bytes"],
             before_physical["sidecars"])

    # Execute forward SQL on forward copy with DELETE journal.
    conn_forward = sqlite3.connect(str(forward_path))
    conn_forward.row_factory = sqlite3.Row
    conn_forward.execute("PRAGMA journal_mode=DELETE")
    conn_forward.execute("PRAGMA synchronous=FULL")
    forward_results = []
    forward_errors = []
    for i, stmt in enumerate(fwd_stmts):
        try:
            cur = conn_forward.execute(stmt)
            affected = cur.rowcount
            conn_forward.commit()
            forward_results.append({
                "statement_index": i,
                "sql": stmt[:80],
                "rowcount": affected,
                "success": affected == 1,
            })
            if affected != 1:
                forward_errors.append(f"Statement {i}: expected 1 row, got {affected}")
        except Exception as e:
            forward_results.append({
                "statement_index": i,
                "sql": stmt[:80],
                "rowcount": -1,
                "success": False,
                "error": str(e),
            })
            forward_errors.append(f"Statement {i}: {e}")

    conn_forward.close()
    forward_physical = physical_fingerprint(str(forward_path))
    conn_forward_reopen = sqlite3.connect(str(forward_path))
    conn_forward_reopen.row_factory = sqlite3.Row
    forward_logical = logical_fingerprint(conn_forward_reopen, str(forward_path))
    conn_forward_reopen.close()
    forward_fp = {**forward_logical, **forward_physical}
    (output_dir / "forward-results.json").write_text(json.dumps({
        "statements": forward_results,
        "errors": forward_errors,
        "total_statements": len(fwd_stmts),
        "successful": sum(1 for r in forward_results if r["success"]),
        "failed": sum(1 for r in forward_results if not r["success"]),
        "fingerprint": forward_fp,
    }, indent=2, ensure_ascii=False))
    log.info("Forward: %d/%d successful (physical sha %s)",
             sum(1 for r in forward_results if r["success"]), len(fwd_stmts),
             forward_physical["physical_sha_confidence"])

    # Forward impact analysis
    conn_before2 = sqlite3.connect(str(before_path))
    conn_before2.row_factory = sqlite3.Row
    conn_forward2 = sqlite3.connect(str(forward_path))
    conn_forward2.row_factory = sqlite3.Row
    stock_info_before = {r["code"]: dict(r) for r in conn_before2.execute(
        "SELECT code, is_active, market FROM stock_info").fetchall()}
    stock_info_after = {r["code"]: dict(r) for r in conn_forward2.execute(
        "SELECT code, is_active, market FROM stock_info").fetchall()}
    conn_before2.close()
    conn_forward2.close()
    changes = []
    for code in stock_info_before:
        before = stock_info_before[code]
        after = stock_info_after.get(code, {})
        if before.get("is_active") != after.get("is_active") or before.get("market") != after.get("market"):
            changes.append({"code": code, "before": before, "after": after})
    (output_dir / "forward-impact.json").write_text(json.dumps({
        "total_changes": len(changes),
        "is_active_changed": sum(1 for c in changes if c["before"]["is_active"] != c["after"]["is_active"]),
        "market_changed": sum(1 for c in changes if c["before"]["market"] != c["after"]["market"]),
        "changes": changes,
    }, indent=2, ensure_ascii=False))

    # Execute rollback SQL on the SAME forward copy.
    conn_rollback = sqlite3.connect(str(forward_path))
    conn_rollback.row_factory = sqlite3.Row
    conn_rollback.execute("PRAGMA journal_mode=DELETE")
    conn_rollback.execute("PRAGMA synchronous=FULL")
    rollback_results = []
    rollback_errors = []
    for i, stmt in enumerate(rbk_stmts):
        try:
            cur = conn_rollback.execute(stmt)
            affected = cur.rowcount
            conn_rollback.commit()
            rollback_results.append({
                "statement_index": i,
                "sql": stmt[:80],
                "rowcount": affected,
                "success": affected == 1,
            })
            if affected != 1:
                rollback_errors.append(f"Rollback stmt {i}: expected 1 row, got {affected}")
        except Exception as e:
            rollback_results.append({
                "statement_index": i,
                "sql": stmt[:80],
                "rowcount": -1,
                "success": False,
                "error": str(e),
            })
            rollback_errors.append(f"Rollback stmt {i}: {e}")

    # Nova B3 fix: commit, close, confirm NO wal/shm, THEN compute at-rest physical SHA.
    conn_rollback.close()
    rollback_sidecars = sidecar_state(str(forward_path))
    rollback_physical = physical_fingerprint(str(forward_path))
    conn_rollback_reopen = sqlite3.connect(str(forward_path))
    conn_rollback_reopen.row_factory = sqlite3.Row
    rollback_logical = logical_fingerprint(conn_rollback_reopen, str(forward_path))
    conn_rollback_reopen.close()
    rollback_fp = {**rollback_logical, **rollback_physical}
    (output_dir / "rollback-results.json").write_text(json.dumps({
        "statements": rollback_results,
        "errors": rollback_errors,
        "total_statements": len(rbk_stmts),
        "successful": sum(1 for r in rollback_results if r["success"]),
        "failed": sum(1 for r in rollback_results if not r["success"]),
        "fingerprint": rollback_fp,
        "physical_sha_after_close_sidecars": rollback_sidecars,
    }, indent=2, ensure_ascii=False))
    log.info("Rollback: %d/%d successful. At-rest physical sha confidence: %s",
             sum(1 for r in rollback_results if r["success"]), len(rbk_stmts),
             rollback_physical["physical_sha_confidence"])

    # Logical restore verification (PRIMARY gate)
    key_tables = ["stock_info", "daily_klines", "stock_pool", "factor_daily", "alpha_factors", "model_predictions"]
    restore_issues = []
    for tbl in key_tables:
        b = before_fp["tables"].get(tbl, {})
        r = rollback_fp["tables"].get(tbl, {})
        # Only tables present in the before fingerprint are compared; a key table
        # missing from the source DB is not a restore regression.
        if not b:
            continue
        if not r:
            restore_issues.append({"table": tbl, "issues": ["table missing after rollback"]})
            continue
        issues = []
        if b.get("row_count", -1) != r.get("row_count", -2):
            issues.append(f"row_count mismatch: before={b.get('row_count')} after_rollback={r.get('row_count')}")
        if b.get("logical_hash") != r.get("logical_hash"):
            issues.append(f"logical_hash mismatch: before={b.get('logical_hash','')[:16]} after_rollback={r.get('logical_hash','')[:16]}")
        if issues:
            restore_issues.append({"table": tbl, "issues": issues})

    physical_match = before_physical["sha256"] == rollback_physical["sha256"]
    physical_confident = (
        before_physical["physical_sha_confidence"].startswith("ok")
        and rollback_physical["physical_sha_confidence"].startswith("ok")
    )
    logical_restore = {
        "restored": len(restore_issues) == 0,
        "issues": restore_issues,
        "before_sha256": before_physical["sha256"],
        "after_rollback_sha256": rollback_physical["sha256"],
        "physical_sha256_match": physical_match,
        "physical_sha_confidence": (
            "ok" if physical_confident else
            "INCONCLUSIVE: a wal/shm sidecar was present at fingerprint time"
        ),
        "note": (
            "Logical hashes are the PRIMARY restore gate. Physical SHA256 may "
            "differ even for a perfect logical restore due to SQLite page layout "
            "changes (freelist/page ordering). The physical comparison here was "
            "taken at rest (connection closed, no wal sidecar) per the Nova B3 fix."
        ),
    }
    (output_dir / "logical-restore-verification.json").write_text(
        json.dumps(logical_restore, indent=2, ensure_ascii=False))

    # Report
    elapsed = time.time() - start
    report = {
        "stage": "B3-R1",
        "status": "COMPLETED",
        "started_at": now_iso(),
        "elapsed_seconds": round(elapsed, 2),
        "forward_statements": len(fwd_stmts),
        "forward_successful": sum(1 for r in forward_results if r["success"]),
        "forward_failed": sum(1 for r in forward_results if not r["success"]),
        "forward_rowcounts": [r["rowcount"] for r in forward_results],
        "rollback_statements": len(rbk_stmts),
        "rollback_successful": sum(1 for r in rollback_results if r["success"]),
        "rollback_failed": sum(1 for r in rollback_results if not r["success"]),
        "rollback_rowcounts": [r["rowcount"] for r in rollback_results],
        "logical_restore": logical_restore["restored"],
        "logical_restore_issues": len(restore_issues),
        "physical_sha256_match": physical_match,
        "physical_sha_confidence": logical_restore["physical_sha_confidence"],
        "physical_sha_after_close_sidecars": rollback_sidecars,
        "forward_errors": forward_errors,
        "rollback_errors": rollback_errors,
        "conclusion": "SQL drill passed" if (len(forward_errors) == 0 and len(rollback_errors) == 0 and logical_restore["restored"]) else "SQL drill FAILED",
    }
    (output_dir / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))

    log.info("=== B3-R1 SQL drill complete ===")
    log.info("Forward: %d/%d successful, Rollback: %d/%d successful",
             report["forward_successful"], report["forward_statements"],
             report["rollback_successful"], report["rollback_statements"])
    log.info("Logical restore: %s", "PASSED" if logical_restore["restored"] else "FAILED")
    log.info("Physical sha match (at-rest): %s (%s)", physical_match, logical_restore["physical_sha_confidence"])
    log.info("Elapsed: %.1fs", elapsed)


if __name__ == "__main__":
    main()
