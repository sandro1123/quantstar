#!/usr/bin/env python3
"""P2-07C-A/B — BJ historical valuation isolated builder.

Stage A (default): exactly 12 approved 92xxxx BSE securities from
`stock_value_em` (the only historical main source), with a crash-robust request
ledger, immutable canonical response cache, per-row provenance, and a versioned
checkpoint supporting resume. Runs A1 (first 4), A2 (resume to 12), A3
(offline rebuild from cache).

Stage B (explicit --stage-b, after an accepted Stage A verdict): full
current-active 92xxxx set with a DYNAMIC identity recomputed from the formal
stock_info table (never a hard-coded count), Stage B hard budgets
(identity<=400, total real<=1500, value_em<=400, other verify<=1000, batch<=20,
timeout<=30s, retry<=1, wall clock<=90min, concurrency=1), a progress snapshot
every 25 codes, a lightweight conservation + quick_check every 50 codes, and
immediate hard-stop on any gate failure. Cross-source audit uses a
deterministic stratified sample of >=30 securities that includes the 6 Stage A
anchors.

Security / isolation contract:
  * Network is OFF by default; real network only with NETWORK_PROBE_ENABLED=1.
  * Writes ONLY to <evidence-root>/build/<stage>-bj-valuation.db and its
    evidence tree. Never the formal DB, never the SH/SZ semantic-fixed
    candidate, never any pre-existing DB.
  * Formal DB read-only mode=ro&immutable=1 + PRAGMA query_only=ON.
  * Stage A and Stage B use different attempt roots and checkpoint versions;
    a Stage A checkpoint is never reused by Stage B (and vice versa).

CLI:
  --identity <json>                    identity lock file
  --official-mapping-evidence <json>    R1 official mapping evidence (read-only)
  --output-db <path>                    candidate DB (default <root>/build/...db)
  --evidence-root <path>                approved evidence root
  --checkpoint <path>                   checkpoint JSON (default build/checkpoint.json)
  --max-codes <n>                       controlled stop after n attempted codes
  --resume                              resume an existing checkpoint at same root
  --offline-response-cache <path>       rebuild offline from canonical cache dir
  --dry-run-plan                        print plan and identity audit; no build/network
  --max-requests <n>                    override total real request cap
  --max-time-minutes <n>                override wall clock cap
  --stage-b                             Stage B mode (full active 92xxxx set)
  --stage-a-verdict <path>              accepted Stage A verdict (required for B)

Exit codes: 0 success; 1 validation/wiring error; 2 budget/hard-stop; 3 resume
rejection; 4 conservation/quick_check hard-gate failure (Stage B).
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import importlib
import inspect
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("build_bj")

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------
NETWORK_ENV_VAR = "NETWORK_PROBE_ENABLED"
FORMAL_DB = os.path.expanduser("~/qs/data/qs.db")
APPROVED_ROOT = os.path.expanduser("~/qs-repair-backups/phase2_p2_07c_a_opencode_20260802")
APPROVED_ROOT_R1 = os.path.expanduser(
    "~/qs-repair-backups/phase2_p2_07c_a_r1_opencode_20260803")
SH_SZ_CANDIDATE = os.path.expanduser(
    "~/qs-repair-backups/phase2_p2_06b_valuation_/semantic_fix_20260801/"
    "forward/valuation.semantic-fixed.db")

APPROVED_CODES = [
    "920017", "920185", "920799", "920808", "920779", "920931",
    "920002", "920003", "920000", "920001", "920005", "920006",
]
ANCHOR_CODES = ["920017", "920185", "920799", "920808", "920002", "920003"]

OFFICIAL_MAPPING_ANCHORS = {
    "920017": "430017",
    "920185": "835185",
    "920799": "830799",
    "920808": "872808",
    "920779": "830779",
    "920931": "872931",
}

STAGE_A_CODES_LIMIT = 12
TOTAL_REAL_CAP = 60
VALUE_EM_CAP = 12
BAIDU_CAP = 24
COMPARISON_CAP = 12
SPOT_BATCH_CAP = 2
WALL_CLOCK_MAX_MIN = 20
SINGLE_REQUEST_TIMEOUT = 30
MAX_RETRY = 1
FUSE_CONSECUTIVE = 3

SOURCE_CAPS = {
    "stock_value_em": VALUE_EM_CAP,
    "stock_zh_valuation_baidu": BAIDU_CAP,
    "stock_zh_valuation_comparison_em": COMPARISON_CAP,
    "stock_bj_a_spot_em": SPOT_BATCH_CAP,
}

# ---------------------------------------------------------------------------
# Stage B (P2-07C-B): full current-active 92xxxx BJ set, isolated candidate.
# Budgets are HARD CAPS (upper bound), not targets. Dynamic identity is
# recomputed from the formal DB; 324 is NOT hard-coded anywhere.
# ---------------------------------------------------------------------------
STAGE_B_ROOT_PREFIX = os.path.expanduser("~/qs-repair-backups/phase2_p2_07c_b")
STAGE_B_IDENTITY_MAX = 400
STAGE_B_TOTAL_REAL_CAP = 1500
STAGE_B_VALUE_EM_CAP = 400
STAGE_B_OTHER_VERIFY_CAP = 1000
STAGE_B_BATCH_CAP = 20
STAGE_B_WALL_CLOCK_MAX_MIN = 90
STAGE_B_CROSS_SOURCE_MIN_SECURITIES = 30
STAGE_B_CROSS_SOURCE_MIN_ROWS = 60
STAGE_B_PROGRESS_SNAPSHOT_EVERY = 25
STAGE_B_CONSERVATION_EVERY = 50
STAGE_B_BUDGET_WARN_PCT = 0.8
STAGE_B_IDENTITY_SQL = (
    "SELECT code, name, market, list_date, is_active FROM stock_info "
    "WHERE is_active = 1 AND market = '北交所' AND code GLOB '92????' ORDER BY code"
)
STAGE_B_CHECKPOINT_VERSION = "p2_07c_b_checkpoint_v1"
STAGE_B_PLAN_VERSION = "p2_07c_b_plan_v1"
STAGE_B_ANCHOR_POLICY_VERSION = "p2_07c_b_anchor_policy_v1"
STAGE_B_NORMALIZATION_VERSION = "p2_07c_b_v1"
STAGE_B_SAMPLE_ANCHORS = list(ANCHOR_CODES)
# accepted Stage A verdict (auditor-emitted) required before Stage B may build
STAGE_A_READY_VERDICT = "STAGE_A_READY_FOR_NOVA_REVIEW"
# M7: Stage A verdict gates that MUST be present AND passed==true before Stage
# B trusts the verdict (not just the verdict string).
STAGE_A_REQUIRED_GATES = [
    "stage_a_identity_ok",
    "only_approved_12_codes",
    "candidate_schema_exact",
    "request_ledger_conserved",
    "row_provenance_complete",
    "cross_source_anchor_consistency",
    "formal_db_unchanged",
    "source_failures_explicit",
]
# which sources count toward the aggregate "other per-code verification" cap
STAGE_B_VERIFY_SOURCES = ["stock_zh_valuation_baidu",
                          "stock_zh_valuation_comparison_em",
                          "stock_bj_a_spot_em"]

STATUS_PROVEN = "PROVEN"
STATUS_UNPROVEN = "UNPROVEN"
STATUS_FIELD_UNAVAILABLE = "FIELD_UNAVAILABLE"
STATUS_LIMITED_SAMPLE = "LIMITED_SAMPLE"
STATUS_ADAPTER_ERROR = "ADAPTER_ERROR"
STATUS_NETWORK_ERROR = "NETWORK_ERROR"
STATUS_RATE_LIMITED = "RATE_LIMITED"
STATUS_CODE_NOT_FOUND = "CODE_NOT_FOUND"
FUSABLE_STATUSES = {STATUS_ADAPTER_ERROR, STATUS_NETWORK_ERROR, STATUS_RATE_LIMITED}

NORMALIZATION_VERSION = "p2_07c_a_v2"
VALUE_EM_COLMAP = {
    "date": "数据日期",
    "pe_ttm": "PE(TTM)",
    "pb_mrq": "市净率",
    "ps_ttm": "市销率",
    "market_cap": "总市值",
    "total_shares": "总股本",
}

# ---------------------------------------------------------------------------
# cross-source (R1): baidu + em comparison adapters, used ONLY for the
# Stage A cross-source audit (§9.2), never to backfill the main history table.
# ---------------------------------------------------------------------------
BAIDU_PE_INDICATOR = "市盈率(TTM)"
BAIDU_PB_INDICATOR = "市净率"
BAIDU_INDICATORS = [
    ("pe_ttm", BAIDU_PE_INDICATOR),
    ("pb_mrq", BAIDU_PB_INDICATOR),
]
# comparison adapter returns a table; the anchor row is the one whose 代码 == code.
COMPARISON_COLMAP = {"pe_ttm": "市盈率-TTM", "pb_mrq": "市净率-MRQ"}
CROSS_SOURCE_ANCHORS = list(ANCHOR_CODES)
CROSS_SOURCE_MIN_ROWS = 12

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS stock_valuation (
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    pe_ttm REAL,
    pb_mrq REAL,
    ps_ttm REAL,
    market_cap REAL,
    total_shares REAL,
    dividend_yield REAL,
    PRIMARY KEY(code, date)
);
CREATE TABLE IF NOT EXISTS valuation_build_state (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS valuation_request_ledger (
    request_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    endpoint TEXT,
    code_or_batch_scope TEXT,
    request_started_at TEXT,
    request_finished_at TEXT,
    attempt_no INTEGER,
    is_retry INTEGER,
    outcome TEXT,
    latency_ms REAL,
    response_sha256 TEXT,
    error_class TEXT,
    error TEXT,
    counted_as_real_request INTEGER
);
CREATE TABLE IF NOT EXISTS valuation_provenance (
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
    PRIMARY KEY(code, date)
);
CREATE TABLE IF NOT EXISTS valuation_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT,
    source TEXT,
    field TEXT,
    error_class TEXT,
    message TEXT,
    raw_value TEXT,
    occurred_at TEXT
);
CREATE TABLE IF NOT EXISTS build_metadata (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

RATE_LIMIT_MARKERS = [
    "rate limit", "rate-limit", "too many requests", "429",
    "访问过于频繁", "访问太频繁", "请求过于频繁", "请求太频繁",
    "操作频繁", "frequent", "frequency limit", "限频", "被限制",
    "please slow down", "slow down", "exceeded", "exceed the",
]
NETWORK_MARKERS = [
    "ConnectionError", "TimeoutError", "RemoteDisconnected",
    "Connection aborted", "Remote end closed", "timed out", "timeout",
    "URLError", "SSLError", "socket", "ConnectionResetError",
    "Max retries", "Failed to establish", "getaddrinfo", "BadStatusLine",
    "IncompleteRead",
]
KNOWN_ABSENCE_KEYS = {"chartInfo", "result"}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8", errors="replace"))


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Return a file SHA-256 with constant memory, including multi-GB DBs."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(OrderedDict.fromkeys(
        str(key) for row in rows for key in row.keys()))
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def _is_rate_limited(msg: str) -> bool:
    low = (msg or "").lower()
    return any(m.lower() in low for m in RATE_LIMIT_MARKERS)


def _is_network_error(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    if name in ("connectionerror", "timeouterror", "urlerror", "sslerror",
                "connectionreseterror", "remotedisconnected", "incompleteread"):
        return True
    return any(m.lower() in msg for m in NETWORK_MARKERS)


class _ExcStub(BaseException):
    def __init__(self, message: str, cls: str = ""):
        super().__init__(message)
        self._n = cls or "Exception"

    def __str__(self):
        return self.args[0] if self.args else ""


def classify_error(exc: BaseException) -> tuple[str, str]:
    msg = f"{type(exc).__name__}: {exc}"
    if _is_rate_limited(msg):
        return "RATE_LIMITED", msg
    if _is_network_error(exc):
        return "NETWORK_ERROR", msg
    m = re.match(r"KeyError:\s*(.+)$", msg)
    if m:
        key = m.group(1).strip().strip("'\"")
        if key in KNOWN_ABSENCE_KEYS:
            return "CODE_NOT_FOUND", msg
        return "ADAPTER_ERROR", f"KeyError on unknown key {key!r}: {msg}"
    return "ADAPTER_ERROR", msg


def classify_str(err: str, exc_cls: str = "") -> str:
    msg = err or ""
    if _is_rate_limited(msg):
        return "RATE_LIMITED"
    if _is_network_error(_ExcStub(msg, exc_cls)):
        return "NETWORK_ERROR"
    m = re.match(r"KeyError:\s*(.+)$", msg)
    if m:
        key = m.group(1).strip().strip("'\"")
        if key in KNOWN_ABSENCE_KEYS:
            return "CODE_NOT_FOUND"
        return "ADAPTER_ERROR"
    return "ADAPTER_ERROR"


def classify_error_str(err: str) -> tuple[str, str]:
    s = classify_str(err)
    return s, err


def _df_to_sha(df: Any) -> str:
    if df is None:
        return sha256_text("null-7c")
    try:
        if hasattr(df, "empty") and df.empty:
            return sha256_text("empty-7c")
        if hasattr(df, "to_json"):
            return sha256_text(str(df.to_json(orient="records", date_format="iso")))
        return sha256_text(str(df))
    except Exception:
        return sha256_text(str(df))


def _sha256_re() -> re.Pattern:
    import re
    return re.compile(r"^[0-9a-f]{64}$")


_REQUEST_ID_RE = None


def request_id_pattern() -> re.Pattern:
    global _REQUEST_ID_RE
    import re
    if _REQUEST_ID_RE is None:
        _REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    return _REQUEST_ID_RE


def _request_id_ok(rid) -> bool:
    """R3-3/R3-4: a legal request_id must be a single, safe basename.

    Rules: str, non-empty, no surrounding whitespace, only [A-Za-z0-9_.-]
    (no '/', '\\\\', path traversal '..'), and must not be '.' or '..'.
    """
    if not isinstance(rid, str):
        return False
    if rid != rid.strip():
        return False
    if not rid:
        return False
    if ".." in rid:
        return False
    return bool(request_id_pattern().match(rid))


def validate_ledger_request_ids(ledger_records: list[dict], label: str = "ledger") -> list[str]:
    """Validate every ledger request_id before a resume can touch state.

    Resume is a trust boundary: duplicate or unsafe IDs must be rejected before
    opening the candidate DB, restoring budget counters, making requests, or
    writing candidate rows. Do not deduplicate here; preserving every failure
    is required so a malformed ledger cannot be made valid by filtering.
    """
    errors: list[str] = []
    first_seen: dict[str, int] = {}
    for index, record in enumerate(ledger_records):
        rid = record.get("request_id") if isinstance(record, dict) else None
        if not _request_id_ok(rid):
            errors.append(f"{label}[{index}]: invalid request_id {rid!r}")
            continue
        if rid in first_seen:
            errors.append(
                f"{label}[{index}]: duplicate request_id {rid!r} "
                f"(first seen at index {first_seen[rid]})"
            )
        else:
            first_seen[rid] = index
    return errors


def validate_cache_sha(ledger_records: list[dict], cache_dir: str | Path,
                       rid: str) -> tuple[bool, str]:
    """R2-1: validate that a single PROVEN ledger entry's response_sha256 is
    a valid lowercase hex64 and matches the actual canonical cache payload SHA.
    Returns (ok, reason)."""
    import gzip, json, re
    sha_pat = re.compile(r"^[0-9a-f]{64}$")
    cached_path = Path(cache_dir) / f"{rid}.canonical.json.gz"
    if not cached_path.exists():
        return False, f"cache missing: {rid}"
    try:
        with gzip.open(cached_path, "rb") as f:
            payload = f.read()
        records = json.loads(payload.decode("utf-8"))
        canonical = json.dumps(records, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        actual_sha = sha256_bytes(canonical)
    except Exception as e:
        return False, f"cache decode failure: {rid}: {e}"
    for rec in ledger_records:
        if rec.get("request_id") == rid:
            expected_sha = rec.get("response_sha256")
            if not isinstance(expected_sha, str) or not sha_pat.match(expected_sha):
                return False, f"invalid response_sha256 for {rid}: {expected_sha!r}"
            if actual_sha != expected_sha:
                return False, f"SHA mismatch for {rid}: cache={actual_sha} ledger={expected_sha}"
            return True, "ok"
    return False, f"request_id not found in ledger: {rid}"


def write_cache_sha_migration_report(evidence_root: Path, ledger_records: list[dict],
                                     cache_dir: str | Path, source_root: str,
                                     version: str = "p2_07c_b_r3_migration_v1") -> dict:
    """R3-5: build the honest + conserved cache-sha migration report.

    Every PROVEN ledger record is enumerated (no filtering by cache existence).
    Each record maps old_sha256 (from source ledger) -> new_sha256 (canonical
    payload SHA in this evidence root). conversion_status is derived honestly:
      unchanged -> old == new (R1/R2 caches were already canonical)
      converted -> old != new
      failed    -> a non-empty error/reason is recorded
    Conservation MUST hold: processed == converted + unchanged + failed and
    processed == total_records == len(records). Writes analysis/
    cache-sha-migration.json and returns the report dict."""
    import gzip
    sha_pat = _sha256_re()
    records = []
    converted = 0
    unchanged = 0
    failed = 0
    processed = 0
    seen_ids = set()
    for rec in ledger_records:
        if rec.get("outcome") != STATUS_PROVEN:
            continue
        rid = rec.get("request_id")
        if not _request_id_ok(rid):
            failed += 1
            processed += 1
            records.append({
                "request_id": rid if isinstance(rid, str) else repr(rid),
                "source": rec.get("source", ""),
                "cache_relative_path": "",
                "old_sha256": rec.get("response_sha256"),
                "new_sha256": None,
                "conversion_status": "failed",
                "error": "invalid request_id",
            })
            continue
        if rid in seen_ids:
            failed += 1
            processed += 1
            records.append({
                "request_id": rid,
                "source": rec.get("source", ""),
                "cache_relative_path": "",
                "old_sha256": rec.get("response_sha256"),
                "new_sha256": None,
                "conversion_status": "failed",
                "error": "duplicate request_id in ledger",
            })
            continue
        seen_ids.add(rid)
        processed += 1
        # R3-5: cache_dir IS the responses directory. Read the cache directly
        # from cache_dir / <rid>.canonical.json.gz (never re-prepend
        # "responses/"). The report's cache_relative_path stays relative to
        # evidence root: responses/<rid>.canonical.json.gz.
        rel_path = f"responses/{rid}.canonical.json.gz"
        cache_path = Path(cache_dir) / f"{rid}.canonical.json.gz"
        old_sha = rec.get("response_sha256")
        try:
            with gzip.open(cache_path, "rb") as f:
                payload = f.read()
            records_json = json.loads(payload.decode("utf-8"))
            canonical = json.dumps(records_json, ensure_ascii=False, sort_keys=True,
                                   default=str).encode("utf-8")
            new_sha = sha256_bytes(canonical)
        except Exception as e:
            failed += 1
            records.append({
                "request_id": rid,
                "source": rec.get("source", ""),
                "cache_relative_path": rel_path,
                "old_sha256": old_sha,
                "new_sha256": None,
                "conversion_status": "failed",
                "error": f"cache read/decode failure: {e}",
            })
            continue
        if not isinstance(old_sha, str) or not sha_pat.match(old_sha):
            failed += 1
            records.append({
                "request_id": rid,
                "source": rec.get("source", ""),
                "cache_relative_path": rel_path,
                "old_sha256": old_sha,
                "new_sha256": new_sha,
                "conversion_status": "failed",
                "error": "invalid old_sha256 in ledger",
            })
            continue
        if old_sha == new_sha:
            status = "unchanged"
            unchanged += 1
        else:
            status = "converted"
            converted += 1
        records.append({
            "request_id": rid,
            "source": rec.get("source", ""),
            "cache_relative_path": rel_path,
            "old_sha256": old_sha,
            "new_sha256": new_sha,
            "conversion_status": status,
        })
    report = {
        "version": version,
        "source_evidence_root": source_root,
        "destination_evidence_root": str(evidence_root.resolve()),
        "generated_at": now_iso(),
        "total_records": processed,
        "processed_records": processed,
        "converted_records": converted,
        "unchanged_records": unchanged,
        "failed_records": failed,
        "conservation": processed == (converted + unchanged + failed)
                        and processed == len(records),
        "records": records,
    }
    analysis_dir = evidence_root / "analysis"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    write_json(analysis_dir / "cache-sha-migration.json", report)
    return report


def _func_accepts_timeout(func) -> bool:
    try:
        return "timeout" in inspect.signature(func).parameters
    except (ValueError, TypeError):
        return False


def df_to_records(df: Any) -> list[dict]:
    if df is None:
        return []
    if hasattr(df, "to_dict"):
        try:
            return df.to_dict(orient="records")
        except Exception:
            df = df.to_records() if hasattr(df, "to_records") else df
    return list(df) if isinstance(df, list) else [{"_raw": str(df)}]


def _finite_float(val: Any) -> float | None:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
            return None
        return float(val)
    if isinstance(val, str):
        s = val.strip()
        if s in ("", "None", "nan", "NaN", "inf", "-inf", "Infinity", "-"):
            return None
        try:
            f = float(s)
        except ValueError:
            return None
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    return None


# ---------------------------------------------------------------------------
# canonical response cache
# ---------------------------------------------------------------------------
class ResponseCache:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, request_id: str) -> Path:
        return self.root / f"{request_id}.canonical.json.gz"

    def canonical_json(self, records: list[dict]) -> bytes:
        return json.dumps(records, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")

    def save(self, request_id: str, records: list[dict]) -> str:
        payload = self.canonical_json(records)
        digest = sha256_bytes(payload)
        with gzip.open(self.path_for(request_id), "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        return digest

    def load(self, request_id: str) -> list[dict]:
        with gzip.open(self.path_for(request_id), "rb") as f:
            return json.loads(f.read().decode("utf-8"))

    def exists(self, request_id: str) -> bool:
        return self.path_for(request_id).exists()

    def sha(self, request_id: str) -> str:
        return sha256_bytes(self.canonical_json(self.load(request_id)))


# ---------------------------------------------------------------------------
# request ledger
# ---------------------------------------------------------------------------
class CounterSafe(dict):
    def incr(self, k, n=1):
        self[k] = self.get(k, 0) + n

    def sorted(self):
        return OrderedDict(sorted(self.items()))


class RequestLedger:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lines = []
        self.parse_errors: list[str] = []
        if self.path.exists():
            for line_number, line in enumerate(
                    self.path.read_text(encoding="utf-8").splitlines(), start=1):
                if line.strip():
                    try:
                        self.lines.append(json.loads(line))
                    except ValueError as exc:
                        self.parse_errors.append(
                            f"line {line_number}: invalid JSON: {exc.msg}"
                        )
        seen = set()
        nd = []
        for rec in self.lines:
            rid = rec.get("request_id")
            if rid and rid not in seen:
                seen.add(rid)
                nd.append(rec)
        self.lines = self.lines

    def append(self, rec: dict) -> None:
        self.lines.append(rec)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def records(self) -> list[dict]:
        return list(self.lines)

    def request_ids(self) -> set:
        return {r.get("request_id") for r in self.lines if r.get("request_id")}

    def real_count(self) -> int:
        return sum(1 for r in self.lines if r.get("counted_as_real_request"))

    def per_source(self) -> dict:
        c = CounterSafe()
        for r in self.lines:
            if r.get("counted_as_real_request"):
                c.incr(r.get("source", ""))
        return dict(c.sorted())


# ---------------------------------------------------------------------------
# atomic write
# ---------------------------------------------------------------------------
def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".ckpt-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(path))
        fd = None
    finally:
        if fd is not None:
            os.close(fd)
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _atomic_publish_no_clobber(temp_path: Path, target: Path) -> None:
    """Publish a same-filesystem temp file atomically without overwriting."""
    os.link(str(temp_path), str(target))
    os.unlink(str(temp_path))


def atomic_write_json_no_clobber(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".json-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None
            json.dump(data, handle, ensure_ascii=False, indent=2, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        _atomic_publish_no_clobber(Path(tmp), path)
    finally:
        if fd is not None:
            os.close(fd)
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def atomic_copy_verified(source: Path, target: Path) -> str:
    """Atomically copy source bytes and prove target SHA equals source SHA."""
    source = Path(source).resolve()
    target = Path(target).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    source_sha = sha256_file(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    if source == target:
        return source_sha
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".ledger-", suffix=".tmp")
    try:
        with source.open("rb") as src, os.fdopen(fd, "wb") as dst:
            fd = None
            shutil.copyfileobj(src, dst, length=1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
        if sha256_file(Path(tmp)) != source_sha:
            raise OSError("temporary copy SHA mismatch")
        _atomic_publish_no_clobber(Path(tmp), target)
        if sha256_file(target) != source_sha:
            raise OSError("materialized copy SHA mismatch")
        return source_sha
    finally:
        if fd is not None:
            os.close(fd)
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------
@dataclass
class Security:
    code: str
    name: str
    market: str
    is_active: int
    list_date: str
    mapping: dict = field(default_factory=dict)


def open_formal_ro() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{FORMAL_DB}?mode=ro&immutable=1", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def load_formal_securities(codes: list[str] | None = None) -> dict[str, dict]:
    codes = codes or APPROVED_CODES
    conn = open_formal_ro()
    try:
        q = ("SELECT code,name,market,list_date,is_active FROM stock_info "
             "WHERE code IN (%s)" % ",".join("?" * len(codes)))
        rows = conn.execute(q, list(codes)).fetchall()
    finally:
        conn.close()
    out = {}
    for r in rows:
        out[r["code"]] = {
            "code": r["code"], "name": r["name"], "market": r["market"],
            "list_date": r["list_date"], "is_active": r["is_active"],
        }
    return out


def security_list_from_formal(codes: list[str] | None = None) -> list[Security]:
    info = load_formal_securities(codes)
    out = []
    for code in (codes or APPROVED_CODES):
        d = info.get(code)
        if d is None:
            out.append(Security(code=code, name="", market="", is_active=0, list_date=""))
        else:
            out.append(Security(code=d["code"], name=d["name"], market=d["market"],
                                is_active=d["is_active"], list_date=d["list_date"]))
    return out


def validate_identity(securities: list[Security],
                      approved: list[str] | None = None,
                      expected: int = STAGE_A_CODES_LIMIT) -> tuple[bool, list[str]]:
    approved = approved or APPROVED_CODES
    errors = []
    if len(securities) != expected:
        errors.append(f"expected {expected} securities, got {len(securities)}")
    codes = [s.code for s in securities]
    if len(set(codes)) != len(codes):
        errors.append("duplicate codes in identity")
    for s in securities:
        if not re.fullmatch(r"92[0-9]{4}", s.code):
            errors.append(f"non-92xxxx code: {s.code}")
        if s.code not in set(approved):
            errors.append(f"code not in approved set: {s.code}")
        if not s.name:
            errors.append(f"empty name for {s.code}")
        if not s.is_active:
            errors.append(f"inactive security: {s.code}")
        if s.code.startswith(("4", "8")):
            errors.append(f"old code used as candidate: {s.code}")
    return len(errors) == 0, errors


def load_all_active_bj_securities() -> list[Security]:
    """Stage B dynamic identity: recompute the full current-active 92xxxx BJ
    list from the formal stock_info table (read-only). 324 is NOT hard-coded;
    the actual count is whatever the query returns today."""
    conn = open_formal_ro()
    try:
        rows = conn.execute(STAGE_B_IDENTITY_SQL).fetchall()
    finally:
        conn.close()
    return [Security(code=r["code"], name=r["name"] or "", market=r["market"] or "",
                     is_active=int(r["is_active"] or 0), list_date=r["list_date"] or "")
            for r in rows]


def validate_identity_stage_b(securities: list[Security],
                              expected_max: int = STAGE_B_IDENTITY_MAX) -> tuple[bool, list[str]]:
    """Dynamic Stage B identity gate (work order §4):
    - count in (0, expected_max]
    - code unique, strict 6 digits, all 92xxxx
    - name non-empty; market == 北交所; is_active == 1
    - no 4/8-prefixed legacy code as candidate primary key
    """
    errors = []
    if not 0 < len(securities) <= expected_max:
        errors.append(f"identity count {len(securities)} not in (0, {expected_max}]")
    codes = [s.code for s in securities]
    if len(set(codes)) != len(codes):
        errors.append("duplicate codes in identity")
    added_code_names: dict[str, str] = {}
    for s in securities:
        if not re.fullmatch(r"92[0-9]{4}", s.code):
            errors.append(f"non-92xxxx code: {s.code}")
        if s.code.startswith(("4", "8")):
            errors.append(f"legacy code used as candidate primary key: {s.code}")
        if not s.name:
            errors.append(f"empty name for {s.code}")
        if s.market != "北交所":
            errors.append(f"non-北交所 market for {s.code}: {s.market}")
        if not s.is_active:
            errors.append(f"inactive security: {s.code}")
        prev = added_code_names.get(s.code)
        if prev not in (None, s.name):
            errors.append(f"same code with different names: {s.code} {prev!r} vs {s.name!r}")
        added_code_names[s.code] = s.name
    return len(errors) == 0, errors


def identity_snapshot_sha(securities: list[Security]) -> str:
    return sha256_text(json.dumps(sorted(s.code for s in securities), ensure_ascii=False))


def write_stage_b_identity(root: Path, securities: list[Security], sha: str,
                           sql: str) -> None:
    """Write identity/bj-active-securities.{json,csv,sha256} + identity-audit.json."""
    ident_dir = root / "identity"
    ident_dir.mkdir(parents=True, exist_ok=True)
    docs = [
        {"code": s.code, "name": s.name, "market": s.market,
         "is_active": s.is_active, "list_date": s.list_date}
        for s in sorted(securities, key=lambda s: s.code)
    ]
    write_json(ident_dir / "bj-active-securities.json",
               {"task": "PHASE2-P2-07C-B", "gate": "G1",
                "recorded_at": now_iso(),
                "query": STAGE_B_IDENTITY_SQL, "source_db_fingerprint": formal_db_sha(),
                "identity_snapshot_sha256": sha, "securities": docs})
    write_csv(ident_dir / "bj-active-securities.csv",
              [{"code": s.code, "name": s.name, "market": s.market,
                "is_active": s.is_active, "list_date": s.list_date}
               for s in sorted(securities, key=lambda s: s.code)])
    (ident_dir / "bj-active-securities.sha256").write_text(
        sha + "  identity/bj-active-securities.json\n", encoding="utf-8")
    write_json(ident_dir / "identity-audit.json", {
        "gate": "G1-identity", "count": len(securities),
        "min": 0, "max": STAGE_B_IDENTITY_MAX,
        "identity_snapshot_sha256": sha, "valid": True})


def formal_db_sha() -> str:
    """Current formal DB SHA256 (read-only stat via file read)."""
    try:
        return sha256_file(Path(FORMAL_DB))
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# Stage B deterministic stratified cross-source sample (§9.5): >= 30 distinct
# securities. Anchors are only required when they are part of the dynamic BJ
# identity; an anchor whose market/identity has changed out of scope (e.g.
# 920808 now SZ) is recorded as an out-of-scope/anomaly and NOT silently
# injected into the sample. Nova adjudicates the business/mapping decision.
# ---------------------------------------------------------------------------
def anchor_policy(securities: list[Security]) -> tuple[list[str], list[str]]:
    """Return (required_anchors_in_identity, out_of_scope_anchors) from the
    dynamic identity. Anchors present in the identity are required; anchors in
    STAGE_B_SAMPLE_ANCHORS that are NOT in the identity are flagged as
    out-of-scope/anomaly so the cross-source gate and strongest objection can
    report them explicitly instead of demanding an impossible anchor."""
    codes = {s.code for s in securities}
    in_id = [a for a in STAGE_B_SAMPLE_ANCHORS if a in codes]
    out = [a for a in STAGE_B_SAMPLE_ANCHORS if a not in codes]
    return in_id, out


def stage_cross_source_sample(securities: list[Security],
                              target: int = STAGE_B_CROSS_SOURCE_MIN_SECURITIES) -> list[str]:
    required_anchors, _ = anchor_policy(securities)
    anchor_set = set(required_anchors)
    by_code = {s.code: s for s in securities if s.code not in anchor_set}
    strata: dict[str, list[str]] = {}
    for code, s in by_code.items():
        y = (s.list_date or "")[:4] or "unknown"
        strata.setdefault(y, []).append(code)
    for y in strata:
        strata[y].sort()
    ordered = sorted(strata.keys())
    need = max(0, target - len(anchor_set))
    pick: list[str] = []
    idx = 0
    while len(pick) < need and strata:
        progressed = False
        for y in ordered:
            if len(pick) >= need:
                break
            bucket = strata.get(y)
            if bucket:
                pick.append(bucket.pop(0))
                progressed = True
        if not progressed:
            break
        idx += 1
    return sorted(anchor_set) + sorted(pick[:need])


def mapping_snapshot_sha(mapping: dict) -> str:
    return sha256_text(json.dumps(mapping, ensure_ascii=False, sort_keys=True))


def read_official_mapping(path: Path) -> dict[str, str]:
    """R1 official-mapping-evidence.json -> {new_code: old_code}."""
    data = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    items = data if isinstance(data, list) else data.get("mappings", data.get("items", data.get("anchors", [])))
    if isinstance(items, dict):
        for k, v in items.items():
            if isinstance(v, dict) and v.get("new_code"):
                out[str(v["new_code"])] = str(v.get("old_code", ""))
            elif isinstance(v, str) and k.startswith("92"):
                out[k] = v
    else:
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            nc = item.get("new_code") or item.get("new")
            oc = item.get("old_code") or item.get("old")
            if nc and oc:
                out[str(nc)] = str(oc)
    return out or dict(OFFICIAL_MAPPING_ANCHORS)


# ---------------------------------------------------------------------------
# budget
# ---------------------------------------------------------------------------
class Budget:
    def __init__(self, total_cap: int = TOTAL_REAL_CAP,
                 per_source_caps: dict | None = None,
                 wallclock_min: int = WALL_CLOCK_MAX_MIN,
                 verify_cap: int | None = None,
                 verify_sources: list[str] | None = None):
        self.total_cap = total_cap
        self.per_source_caps = per_source_caps or dict(SOURCE_CAPS)
        # Stage B: aggregate cap over "other per-code verification" sources
        self.verify_cap = verify_cap
        self.verify_sources = set(verify_sources or [])
        self.total_used = 0
        self.per_source_used = CounterSafe()
        self.consecutive_fail = CounterSafe()
        self.fused = set()
        self.deadline = time.monotonic() + wallclock_min * 60

    def try_allow(self, src: str, permits: int = 1) -> bool:
        if src in self.fused:
            return False
        if self.total_used + permits > self.total_cap:
            return False
        if self.per_source_used.get(src, 0) + permits > self.per_source_caps.get(src, 999):
            return False
        if self.verify_cap is not None and src in self.verify_sources:
            other_used = sum(self.per_source_used.get(s, 0) for s in self.verify_sources)
            if other_used + permits > self.verify_cap:
                return False
        if time.monotonic() >= self.deadline:
            return False
        return True

    def consume(self, src: str, permits: int, ok: bool) -> None:
        self.total_used += permits
        self.per_source_used.incr(src, permits)
        if ok:
            self.consecutive_fail[src] = 0
        else:
            self.consecutive_fail.incr(src)
            if self.consecutive_fail[src] >= FUSE_CONSECUTIVE:
                self.fused.add(src)

    def snapshot(self) -> dict:
        return {
            "total_used": self.total_used, "total_cap": self.total_cap,
            "per_source_used": dict(self.per_source_used),
            "consecutive_fail": dict(self.consecutive_fail),
            "per_source_caps": dict(self.per_source_caps),
            "fused": sorted(self.fused),
            "deadline_remaining_s": round(max(0, self.deadline - time.monotonic()), 3),
        }

    def restore_snapshot(self, rc: dict) -> None:
        """M2/M6: restore runtime counters + fuse state from a checkpoint
        snapshot so resume conserves the FULL run budget (including pre-resume
        requests) and does not reset to zero.

        M6 strict validation: every required field must be present with the
        correct JSON type. Counts must be JSON integers (Python bool and any
        float — including whole floats, decimals, NaN, Infinity — are
        rejected; never truncated). deadline_remaining_s must be a finite
        non-negative number. fused must be a string list. The snapshot is fully
        validated BEFORE mutating any state, so a bad snapshot leaves the
        existing budget untouched and raises ValueError for the caller to stop
        non-zero (BLOCKED_INVALID_BUDGET_SNAPSHOT)."""
        if not isinstance(rc, dict) or not rc:
            raise ValueError("budget snapshot missing or not an object")

        def _bad(field: str, why: str) -> ValueError:
            return ValueError(f"request_counters.{field} {why}")

        # total_used: required JSON integer (no bool/float/str/NaN/Inf)
        if "total_used" not in rc:
            raise _bad("total_used", "missing")
        total_used = rc["total_used"]
        if isinstance(total_used, bool) or not isinstance(total_used, int):
            raise _bad("total_used", "must be a JSON integer")
        if total_used < 0:
            raise _bad("total_used", "negative")

        # per_source_used: required dict of JSON integers
        if "per_source_used" not in rc or not isinstance(rc["per_source_used"], dict):
            raise _bad("per_source_used", "missing/not an object")
        parsed_per_src: dict = {}
        for s, v in rc["per_source_used"].items():
            if isinstance(v, bool) or not isinstance(v, int):
                raise ValueError(f"request_counters.per_source_used[{s}] must be a JSON integer")
            if v < 0:
                raise ValueError(f"request_counters.per_source_used[{s}] negative")
            parsed_per_src[str(s)] = v
        if sum(parsed_per_src.values()) != total_used:
            raise ValueError(
                f"non-conserving budget: per-source sum {sum(parsed_per_src.values())} "
                f"!= total_used {total_used}")

        # consecutive_fail: required dict of JSON integers
        if "consecutive_fail" not in rc or not isinstance(rc["consecutive_fail"], dict):
            raise _bad("consecutive_fail", "missing/not an object")
        parsed_cf: dict = {}
        for s, v in rc["consecutive_fail"].items():
            if isinstance(v, bool) or not isinstance(v, int):
                raise ValueError(f"request_counters.consecutive_fail[{s}] must be a JSON integer")
            if v < 0:
                raise ValueError(f"request_counters.consecutive_fail[{s}] negative")
            parsed_cf[str(s)] = v

        # fused: required string list
        if "fused" not in rc or not isinstance(rc["fused"], list):
            raise _bad("fused", "missing/not a list")
        for c in rc["fused"]:
            if not isinstance(c, str):
                raise ValueError("request_counters.fused entries must be strings")

        # deadline_remaining_s: required finite non-negative number (int/float
        # accepted; string/NaN/Infinity/negative rejected)
        if "deadline_remaining_s" not in rc:
            raise _bad("deadline_remaining_s", "missing")
        dl = rc["deadline_remaining_s"]
        if isinstance(dl, bool) or not isinstance(dl, (int, float)):
            raise _bad("deadline_remaining_s", "must be a finite number")
        if not math.isfinite(dl) or dl < 0:
            raise _bad("deadline_remaining_s", "must be finite and >= 0")

        # All validations passed; only now mutate state.
        self.total_used = total_used
        self.per_source_used = CounterSafe(parsed_per_src)
        self.consecutive_fail = CounterSafe(parsed_cf)
        self.fused = set(rc["fused"])
        self._restored_deadline_remaining = float(dl)

    def restored_deadline_remaining(self) -> float:
        return getattr(self, "_restored_deadline_remaining", None)

    def set_deadline_remaining(self, remaining_s: float) -> None:
        if isinstance(remaining_s, bool) or not isinstance(remaining_s, (int, float)):
            raise ValueError("remaining_s must be a finite number")
        if not math.isfinite(remaining_s) or remaining_s < 0:
            raise ValueError("remaining_s must be finite and >= 0")
        self.deadline = time.monotonic() + float(remaining_s)


def make_stage_b_budget(wallclock_min: int = STAGE_B_WALL_CLOCK_MAX_MIN) -> Budget:
    return Budget(
        total_cap=STAGE_B_TOTAL_REAL_CAP,
        per_source_caps={
            "stock_value_em": STAGE_B_VALUE_EM_CAP,
            "stock_zh_valuation_baidu": STAGE_B_OTHER_VERIFY_CAP,
            "stock_zh_valuation_comparison_em": STAGE_B_OTHER_VERIFY_CAP,
            "stock_bj_a_spot_em": STAGE_B_BATCH_CAP,
        },
        wallclock_min=wallclock_min,
        verify_cap=STAGE_B_OTHER_VERIFY_CAP,
        verify_sources=STAGE_B_VERIFY_SOURCES,
    )


# ---------------------------------------------------------------------------
# network adapters (bounded, OFF by default)
# ---------------------------------------------------------------------------
def import_akshare():
    try:
        return importlib.import_module("akshare")
    except Exception as e:  # noqa: BLE001
        log.warning("akshare import failed: %s", e)
        return None


def _datacall(ak: Any, src: str, req: dict) -> tuple[Any, str | None, str]:
    if ak is None:
        return None, "akshare unavailable", "ADAPTER_ERROR"
    fn = getattr(ak, src, None)
    if fn is None:
        return None, f"akshare has no {src}", "ADAPTER_ERROR"
    try:
        if src == "stock_value_em":
            df = fn(symbol=req["code"])
        elif src == "stock_zh_valuation_baidu":
            df = fn(symbol=req["code"], indicator=req["indicator"], period="近一年")
        elif src == "stock_zh_valuation_comparison_em":
            df = fn(symbol="BJ" + req["code"])
        elif src == "stock_bj_a_spot_em":
            df = fn()
        else:
            df = fn()
        return df, None, ""
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}", type(e).__name__


def _call_with_retry(ak: Any, src: str, req: dict,
                     budget: Budget) -> tuple[Any, str | None, int, bool, float, str]:
    started = time.monotonic()
    df, err, cls = _datacall(ak, src, req)
    attempt, is_retry = 1, False
    latency = (time.monotonic() - started) * 1000
    if df is None and err and _is_network_error(_ExcStub(err, cls)):
        attempt = 2
        is_retry = True
        df, err, cls = _datacall(ak, src, req)
        latency = (time.monotonic() - started) * 1000
    status = "PROVEN" if err is None else classify_str(err, cls)
    return df, err, attempt, is_retry, latency, status


# ---------------------------------------------------------------------------
# Stage A verdict structural gate (M7). Stage B must NOT trust only the
# verdict string; a forged JSON string must be rejected.
# ---------------------------------------------------------------------------
def validate_stage_a_verdict(verdict_path: Path) -> tuple[bool, str]:
    """Verify the Stage A verdict is structurally authentic, not just a string:
      - file exists and parses as JSON
      - verdict == STAGE_A_READY_VERDICT
      - gates is a list of objects
      - every STAGE_A_REQUIRED_GATES present with passed == true
      - strongest_objection present (a string)
    Returns (ok, reason)."""
    if verdict_path is None or not Path(verdict_path).exists():
        return False, "verdict path missing"
    try:
        va = json.loads(Path(verdict_path).read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        return False, f"verdict not valid JSON: {e}"
    if not isinstance(va, dict):
        return False, "verdict root not an object"
    if va.get("verdict") != STAGE_A_READY_VERDICT:
        return False, f"verdict value {va.get('verdict')!r} != {STAGE_A_READY_VERDICT!r}"
    if "strongest_objection" not in va or not isinstance(va.get("strongest_objection"), str):
        return False, "verdict missing string strongest_objection"
    gates = va.get("gates")
    if not isinstance(gates, list) or any(not isinstance(g, dict) for g in gates):
        return False, "verdict gates not a list of objects"
    by_name = {g.get("gate"): g for g in gates if isinstance(g, dict) and g.get("gate")}
    problems = []
    for name in STAGE_A_REQUIRED_GATES:
        g = by_name.get(name)
        if g is None:
            problems.append(f"missing gate {name}")
        elif g.get("passed") is not True:
            problems.append(f"gate {name} not passed")
    if problems:
        return False, "; ".join(problems)
    return True, "Stage A verdict structurally authentic"


# ---------------------------------------------------------------------------
# G0 baseline: complete formal DB fingerprint (R1-2). Must be recorded before
# any B1 build, and must be fail-closed (no None wildcard).
# ---------------------------------------------------------------------------
def _g0_file_state(path: Path) -> dict:
    if not path.exists():
        return {"exists": False, "inode": 0, "size": 0, "mtime_ns": 0}
    stat = path.stat()
    return {"exists": True, "inode": stat.st_ino, "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns}


def write_g0_baseline(evidence_root: Path, formal_db: Path | None = None) -> dict:
    """Capture a stable formal-DB G0 and publish it atomically, once."""
    fdb = formal_db or Path(FORMAL_DB)
    p = Path(fdb).resolve()
    if not p.exists():
        raise FileNotFoundError(f"formal DB not reachable: {p}")
    wal_path = p.parent / f"{p.name}-wal"
    shm_path = p.parent / f"{p.name}-shm"
    before = {"db": _g0_file_state(p), "wal": _g0_file_state(wal_path),
              "shm": _g0_file_state(shm_path)}
    formal_sha = sha256_file(p)
    after = {"db": _g0_file_state(p), "wal": _g0_file_state(wal_path),
             "shm": _g0_file_state(shm_path)}
    if before != after:
        raise OSError("formal DB/WAL/SHM changed while G0 SHA was captured")
    wal_exists = after["wal"]["exists"]
    shm_exists = after["shm"]["exists"]
    import subprocess
    try:
        head_result = subprocess.run(["git", "rev-parse", "HEAD"],
                                     capture_output=True, text=True, timeout=10)
        status_result = subprocess.run(["git", "status", "--short"],
                                       capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise OSError(f"cannot capture Git G0: {exc}") from exc
    if head_result.returncode != 0 or status_result.returncode != 0:
        raise OSError("cannot capture Git G0: git command returned non-zero")
    git_head = head_result.stdout.strip()
    git_status_count = len([line for line in status_result.stdout.splitlines()
                            if line.strip()])
    baseline = {
        "formal_db_path": str(p.resolve()),
        "formal_db_sha256": formal_sha,
        "formal_db_size": after["db"]["size"],
        "formal_db_mtime_ns": after["db"]["mtime_ns"],
        "wal_exists": wal_exists,
        "wal_size": after["wal"]["size"],
        "wal_mtime_ns": after["wal"]["mtime_ns"],
        "shm_exists": shm_exists,
        "shm_size": after["shm"]["size"],
        "shm_mtime_ns": after["shm"]["mtime_ns"],
        "git_head": git_head,
        "git_status_count": git_status_count,
        "recorded_at": now_iso(),
    }
    errors = validate_g0_baseline_shape(baseline)
    if errors:
        raise ValueError("captured G0 invalid: " + "; ".join(errors))
    base_dir = evidence_root / "baseline"
    base_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json_no_clobber(base_dir / "g0-baseline.json", baseline)
    return baseline


def validate_g0_baseline_shape(baseline: Any) -> list[str]:
    """Validate the immutable G0 structure before a Stage B resume."""
    if not isinstance(baseline, dict):
        return ["root is not an object"]
    errors = []
    string_fields = ("formal_db_path", "formal_db_sha256", "git_head", "recorded_at")
    int_fields = ("formal_db_size", "formal_db_mtime_ns", "wal_size",
                  "wal_mtime_ns", "shm_size", "shm_mtime_ns", "git_status_count")
    bool_fields = ("wal_exists", "shm_exists")
    for field in string_fields + int_fields + bool_fields:
        if field not in baseline:
            errors.append(f"missing {field}")
    for field in string_fields:
        if field in baseline and (not isinstance(baseline[field], str) or
                                  not baseline[field]):
            errors.append(f"{field} must be a non-empty string")
    for field in int_fields:
        value = baseline.get(field)
        if field in baseline and (type(value) is not int or value < 0):
            errors.append(f"{field} must be an int >= 0")
    for field in bool_fields:
        if field in baseline and type(baseline[field]) is not bool:
            errors.append(f"{field} must be bool")
    path_value = baseline.get("formal_db_path")
    if isinstance(path_value, str) and path_value:
        path = Path(path_value)
        if not path.is_absolute() or path.resolve() != path or ".." in path.parts:
            errors.append("formal_db_path must be a normalized absolute path")
    sha_value = baseline.get("formal_db_sha256")
    if isinstance(sha_value, str) and not re.fullmatch(r"[0-9a-f]{64}", sha_value):
        errors.append("formal_db_sha256 must be lowercase SHA-256")
    head_value = baseline.get("git_head")
    if isinstance(head_value, str) and not re.fullmatch(r"[0-9a-f]{40}", head_value):
        errors.append("git_head must be a 40-character lowercase SHA")
    return errors


def ensure_stage_b_g0(evidence_root: Path, resume: bool) -> tuple[bool, str]:
    """Create G0 once before state open, or require its immutable resume copy."""
    path = evidence_root / "baseline" / "g0-baseline.json"
    if resume:
        if not path.is_file():
            return False, "resume evidence root has no baseline/g0-baseline.json"
        try:
            baseline = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return False, f"resume G0 is unreadable: {exc}"
        errors = validate_g0_baseline_shape(baseline)
        if errors:
            return False, "resume G0 invalid: " + "; ".join(errors)
        return True, "existing G0 retained"
    if path.exists():
        return False, "new Stage B run refuses to overwrite an existing G0"
    try:
        baseline = write_g0_baseline(evidence_root)
    except (OSError, ValueError) as exc:
        return False, f"cannot capture G0: {exc}"
    errors = validate_g0_baseline_shape(baseline)
    if errors:
        return False, "captured G0 invalid: " + "; ".join(errors)
    return True, "new G0 captured"


def _plan_sha(plan: dict) -> str:
    """Deterministic plan SHA over versioned/ordering fields only (excludes
    created_at so the same plan has a stable SHA across A1/A2/A3 resume).

    Stage B: must include the anchor-policy stable fields (required anchors,
    out-of-scope anchors, and the FINAL cross-source sample, all frozen at
    first dry-run). Any drift in identity/anchor/sample changes the SHA and
    therefore rejects an incompatible resume (BLOCKED_BASELINE_DRIFT)."""
    stable = {
        "version": plan.get("version"),
        "anchor_policy_version": plan.get("anchor_policy_version"),
        "plan_codes": list(plan.get("plan_codes", [])),
        "anchor_codes": list(plan.get("anchor_codes", [])),
        "required_anchor_codes": list(plan.get("required_anchor_codes", [])),
        "out_of_scope_anchor_codes": list(plan.get("out_of_scope_anchor_codes", [])),
        "cross_source_sample_codes": list(plan.get("cross_source_sample_codes", [])),
        "identity_snapshot_sha256": plan.get("identity_snapshot_sha256"),
        "total_codes": plan.get("total_codes"),
        "historical_main_source": plan.get("historical_main_source"),
    }
    return sha256_text(json.dumps(stable, ensure_ascii=False, sort_keys=True))


def build_plan(securities: list[Security],
               max_codes: int | None = STAGE_A_CODES_LIMIT) -> dict:
    # The plan ALWAYS lists the full ordered set code set (work-order §4 anchors
    # first, samples last). `max_codes` is a run-time ATTEMPT cap (Run A1=4);
    # it must NOT alter plan_codes, else resume A2 (different max_codes) would
    # change plan_sha256 and reject a valid resume.
    order = [s.code for s in securities]
    return {
        "version": "p2_07c_a_plan_v1",
        "created_at": now_iso(),
        "total_codes": len(order),
        "max_codes": max_codes,
        "plan_codes": order,
        "anchor_codes": ANCHOR_CODES,
        "historical_main_source": "stock_value_em",
    }


def build_plan_stage_b(securities: list[Security], identity_sha: str | None = None) -> dict:
    """Stage B plan: the full current-active 92xxxx identity (dynamic count),
    no attempt cap. Same plan_sha stability rules as Stage A.

    Anchor policy (M1/M3): required + out-of-scope anchors are computed from
    the dynamic identity (not a fixed "must all exist" set), and the FINAL
    cross-source sample is frozen here on first dry-run so resume never
    re-samples. identity_snapshot_sha256 binds the sample to the exact identity
    snapshot. All of these are stable fields consumed by _plan_sha, so any
    identity/anchor/sample drift changes plan_sha256 and blocks an incompatible
    resume (BLOCKED_BASELINE_DRIFT).
    """
    if identity_sha is None:
        identity_sha = identity_snapshot_sha(securities)
    required_anchors, out_of_scope = anchor_policy(securities)
    sample = stage_cross_source_sample(securities)
    order = [s.code for s in securities]
    return {
        "version": STAGE_B_PLAN_VERSION,
        "anchor_policy_version": STAGE_B_ANCHOR_POLICY_VERSION,
        "created_at": now_iso(),
        "total_codes": len(order),
        "max_codes": None,
        "plan_codes": order,
        "anchor_codes": required_anchors,
        "required_anchor_codes": sorted(required_anchors),
        "out_of_scope_anchor_codes": sorted(out_of_scope),
        "cross_source_sample_codes": sorted(sample),
        "identity_snapshot_sha256": identity_sha,
        "historical_main_source": "stock_value_em",
    }


def make_checkpoint_stage_b(plan: dict, identity_sha: str, mapping_sha: str,
                            builder_sha: str, schema_sha: str, started_at: str,
                            completed_codes: list[str], in_progress_code: str | None,
                            completed_operations: list[str], next_seq: int,
                            counters: dict, source_state: dict,
                            candidate_counters: dict, stage: str = "B",
                            factual_request_counters: dict | None = None) -> dict:
    cp = make_checkpoint(plan, identity_sha, mapping_sha, builder_sha, schema_sha,
                         started_at, completed_codes, in_progress_code,
                         completed_operations, next_seq, counters, source_state,
                         candidate_counters,
                         factual_request_counters=factual_request_counters)
    cp["version"] = STAGE_B_CHECKPOINT_VERSION
    cp["stage"] = stage
    return cp


# ---------------------------------------------------------------------------
# normalization
# ---------------------------------------------------------------------------
def normalize_value_em(code: str, df: Any, adapter_sha: str,
                       identity_sha: str, mapping_sha: str,
                       request_id: str,
                       normalization_version: str = NORMALIZATION_VERSION) -> tuple[list[dict], list[dict]]:
    records = df_to_records(df)
    rows = []
    errors = []
    for rec in records:
        date = str(rec.get(VALUE_EM_COLMAP["date"], "") or "")[:10]
        if len(date) != 10 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            errors.append({"code": code, "date": "", "source": "stock_value_em",
                           "field": "date", "error_class": "PARSE",
                           "message": "invalid date", "raw": str(rec)})
            continue
        row = {"code": code, "date": date, "pe_ttm": None, "pb_mrq": None,
               "ps_ttm": None, "market_cap": None, "total_shares": None,
               "dividend_yield": None}
        field_errors = []
        for field in ("pe_ttm", "pb_mrq", "ps_ttm", "market_cap", "total_shares"):
            col = VALUE_EM_COLMAP[field]
            raw = rec.get(col)
            fval = _finite_float(raw)
            if fval is None:
                if raw not in (None, ""):
                    field_errors.append((field, str(raw)))
            else:
                row[field] = fval
        raw_sha = sha256_text(json.dumps(rec, ensure_ascii=False, sort_keys=True, default=str))
        norm_sha = sha256_text(json.dumps(row, ensure_ascii=False, sort_keys=True))
        provenance = {
            "code": code, "date": date, "request_id": request_id,
            "source": "stock_value_em",
            "source_column_map": json.dumps(VALUE_EM_COLMAP, ensure_ascii=False),
            "raw_row_sha256": raw_sha, "normalized_record_sha256": norm_sha,
            "retrieved_at": now_iso(), "adapter_sha256": adapter_sha,
            "identity_snapshot_sha256": identity_sha,
            "mapping_snapshot_sha256": mapping_sha,
            "normalization_version": normalization_version,
        }
        rows.append({"row": row, "provenance": provenance})
        for field, raw in field_errors:
            errors.append({"code": code, "date": date, "source": "stock_value_em",
                           "field": field, "error_class": "PARSE",
                           "message": "non-finite/unparseable", "raw": raw})
    return rows, errors


# ---------------------------------------------------------------------------
# cross-source normalization (baidu / comparison) — audit-only, R1
# ---------------------------------------------------------------------------
def normalize_baidu_series(code: str, indicator: str, records: list[dict],
                           adapter_sha: str, identity_sha: str, mapping_sha: str,
                           request_id: str) -> tuple[list[dict], list[dict]]:
    """Normalize a baidu PE/PB series response into per-row provenance.
    records: [{date, value}, ...] (date column 'date', value column 'value')."""
    rows = []
    errors = []
    for rec in records:
        date = str(rec.get("date", "") or "")[:10]
        if len(date) != 10 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            errors.append({"code": code, "date": "", "source": "stock_zh_valuation_baidu",
                           "field": "date", "error_class": "PARSE",
                           "message": "invalid date", "raw": str(rec)})
            continue
        fval = _finite_float(rec.get("value"))
        if fval is None:
            errors.append({"code": code, "date": date, "source": "stock_zh_valuation_baidu",
                           "field": indicator, "error_class": "PARSE",
                           "message": "non-finite value", "raw": str(rec.get("value"))})
            continue
        prov = {
            "code": code, "date": date, "request_id": request_id,
            "source": "stock_zh_valuation_baidu",
            "source_column_map": json.dumps({"date": "date", "value": indicator},
                                            ensure_ascii=False),
            "raw_row_sha256": sha256_text(json.dumps(rec, ensure_ascii=False,
                                                     sort_keys=True, default=str)),
            "normalized_record_sha256": sha256_text(json.dumps(
                {"code": code, "date": date, "indicator": indicator, "value": fval},
                ensure_ascii=False, sort_keys=True, default=str)),
            "retrieved_at": now_iso(), "adapter_sha256": adapter_sha,
            "identity_snapshot_sha256": identity_sha,
            "mapping_snapshot_sha256": mapping_sha,
            "normalization_version": NORMALIZATION_VERSION,
        }
        rows.append({"row": {"code": code, "date": date, "indicator": indicator,
                             "value": fval}, "provenance": prov})
    return rows, errors


def normalize_comparison_row(code: str, rec: dict, adapter_sha: str,
                             identity_sha: str, mapping_sha: str,
                             request_id: str) -> tuple[dict, dict]:
    """Normalize one em-comparison snapshot row (PE-TTM / PB-MRQ) for an anchor.
    Returns (row, provenance); row fields NULL when not present / non-finite.
    A single snapshot produces one provenance row at the retrieved date."""
    row = {"code": code, "date": rec.get("_retrieved_date", ""),
           "pe_ttm": None, "pb_mrq": None}
    # only map explicitly-TTM / -MRQ fields; ordinary PS never mapped to ps_ttm
    for field, col in COMPARISON_COLMAP.items():
        fval = _finite_float(rec.get(col))
        if fval is not None:
            row[field] = fval
    prov = {
        "code": code, "date": row["date"], "request_id": request_id,
        "source": "stock_zh_valuation_comparison_em",
        "source_column_map": json.dumps(COMPARISON_COLMAP, ensure_ascii=False),
        "raw_row_sha256": sha256_text(json.dumps(rec, ensure_ascii=False,
                                                 sort_keys=True, default=str)),
        "normalized_record_sha256": sha256_text(json.dumps(row, ensure_ascii=False,
                                                           sort_keys=True, default=str)),
"retrieved_at": now_iso(), "adapter_sha256": adapter_sha,
            "identity_snapshot_sha256": identity_sha,
            "mapping_snapshot_sha256": mapping_sha,
            "normalization_version": NORMALIZATION_VERSION,
        }
    return row, prov


# ---------------------------------------------------------------------------
# cross-source collection (R1): baidu PE/PB + em comparison for the 6 anchors.
# Responses are written to the immutable response cache and ledger so the
# auditor can recompute same-date comparisons from raw evidence. This NEVER
# backfills the main history table.
# ---------------------------------------------------------------------------
def collect_cross_source(ak: Any, budget: Budget, ledger: RequestLedger,
                         cache: ResponseCache, anchors: list[str],
                         adapter_sha: str, identity_sha: str, mapping_sha: str,
                         network_off: bool) -> dict:
    """Collect baidu PE(TTM), baidu PB and em-comparison for each anchor.

    Returns {} when network is off (auditor then correctly reports the cross
    source gate as NOT provable). Otherwise returns a summary keyed by request:
      {"baidu": {code: {field: value...}}, "comparison": {code: {...}}}
    """
    if network_off:
        return {}
    summary: dict = {"baidu": {}, "comparison": {}, "failures": []}
    started_at = now_iso()

    for code in anchors:
        # --- baidu PE and PB (2 requests per anchor) ---
        per = {}
        for field, indicator in BAIDU_INDICATORS:
            req = {"code": code, "indicator": indicator}
            if not budget.try_allow("stock_zh_valuation_baidu", 1):
                summary["failures"].append(f"{code} baidu budget exhausted")
                continue
            df, err, attempt, is_retry, latency, status = _call_with_retry(
                ak, "stock_zh_valuation_baidu", req, budget)
            budget.consume("stock_zh_valuation_baidu", 1, ok=(status == STATUS_PROVEN))
            rid = f"baidu-{code}-{field}"
            # R1-1: SHA must match the actual canonical cache content. Compute
            # from the cache BEFORE writing the ledger entry.
            if df is not None and not getattr(df, "empty", False) and err is None:
                recs = df_to_records(df)
                sha = cache.save(rid, recs)
            else:
                sha = _df_to_sha(df)
                recs = []
            ledger.append({
                "request_id": rid, "source": "stock_zh_valuation_baidu",
                "endpoint": "akshare.stock_zh_valuation_baidu",
                "code_or_batch_scope": code,
                "request_started_at": started_at, "request_finished_at": now_iso(),
                "attempt_no": attempt, "is_retry": int(is_retry),
                "outcome": status, "latency_ms": round(latency, 3),
                "response_sha256": sha,
                "error_class": "" if err is None else classify_str(err),
                "error": err or "", "counted_as_real_request": 1,
            })
            if err is not None:
                summary["failures"].append(f"{code} baidu {field}: {err}")
                continue
            if df is not None and getattr(df, "empty", False) is False:
                rows, _ = normalize_baidu_series(code, indicator, recs,
                                                 adapter_sha, identity_sha,
                                                 mapping_sha, rid)
                if rows:
                    latest = rows[-1]["row"]
                    per[field] = {"date": latest["date"], "value": latest["value"],
                                  "request_id": rid}
            else:
                summary["failures"].append(f"{code} baidu {field} empty")
        summary["baidu"][code] = per

        # --- em comparison snapshot (1 request per anchor) ---
        req = {"code": code}
        if not budget.try_allow("stock_zh_valuation_comparison_em", 1):
            summary["failures"].append(f"{code} comparison budget exhausted")
            continue
        df, err, attempt, is_retry, latency, status = _call_with_retry(
            ak, "stock_zh_valuation_comparison_em", req, budget)
        budget.consume("stock_zh_valuation_comparison_em", 1,
                       ok=(status == STATUS_PROVEN))
        rid = f"cmp-{code}"
        if df is not None and not getattr(df, "empty", False) and err is None:
            recs = df_to_records(df)
            sha = cache.save(rid, recs)
        else:
            sha = _df_to_sha(df)
            recs = []
        ledger.append({
            "request_id": rid, "source": "stock_zh_valuation_comparison_em",
            "endpoint": "akshare.stock_zh_valuation_comparison_em",
            "code_or_batch_scope": code, "request_started_at": started_at,
            "request_finished_at": now_iso(), "attempt_no": attempt,
            "is_retry": int(is_retry), "outcome": status,
            "latency_ms": round(latency, 3), "response_sha256": sha,
            "error_class": "" if err is None else classify_str(err),
            "error": err or "", "counted_as_real_request": 1,
        })
        if err is not None:
            summary["failures"].append(f"{code} comparison: {err}")
            continue
        if df is not None and getattr(df, "empty", False) is False:
            anchor_row = next((r for r in recs
                               if str(r.get("代码", "") or "") == code), None)
            if anchor_row is None:
                summary["failures"].append(f"{code} comparison no anchor row")
                continue
            row, _ = normalize_comparison_row(code, anchor_row, adapter_sha,
                                              identity_sha, mapping_sha, rid)
            summary["comparison"][code] = {"row": row, "request_id": rid}
    return summary


def rebuild_cross_source_from_cache(ledger_records: list[dict], cache_dir: Path,
                                    sample_codes: list[str], adapter_sha: str,
                                    identity_sha: str, mapping_sha: str) -> dict:
    """Deterministically derive Stage B cross-source evidence from raw cache."""
    cache_dir = Path(cache_dir).resolve()
    by_id = {r.get("request_id"): r for r in ledger_records
             if isinstance(r, dict) and _request_id_ok(r.get("request_id"))}
    summary: dict = {"baidu": {}, "comparison": {}, "failures": []}
    for code in sample_codes:
        per = {}
        for field, indicator in BAIDU_INDICATORS:
            rid = f"baidu-{code}-{field}"
            rec = by_id.get(rid)
            if not rec or rec.get("source") != "stock_zh_valuation_baidu" or rec.get("outcome") != STATUS_PROVEN:
                summary["failures"].append(f"{code} missing PROVEN {rid}")
                continue
            ok, reason = validate_cache_sha(ledger_records, cache_dir, rid)
            if not ok:
                summary["failures"].append(f"{code} {rid}: {reason}")
                continue
            rows, errors = normalize_baidu_series(
                code, indicator, ResponseCache(cache_dir).load(rid), adapter_sha,
                identity_sha, mapping_sha, rid)
            if errors or not rows:
                summary["failures"].append(
                    f"{code} {rid}: normalization errors={len(errors)} rows={len(rows)}")
                continue
            latest = max((item["row"] for item in rows), key=lambda row: row["date"])
            per[field] = {"date": latest["date"], "value": latest["value"],
                          "request_id": rid}
        summary["baidu"][code] = per

        rid = f"cmp-{code}"
        rec = by_id.get(rid)
        if not rec or rec.get("source") != "stock_zh_valuation_comparison_em" or rec.get("outcome") != STATUS_PROVEN:
            summary["failures"].append(f"{code} missing PROVEN {rid}")
            continue
        ok, reason = validate_cache_sha(ledger_records, cache_dir, rid)
        if not ok:
            summary["failures"].append(f"{code} {rid}: {reason}")
            continue
        records = ResponseCache(cache_dir).load(rid)
        target = next((row for row in records
                       if str(row.get("代码", "") or "") == code), None)
        if target is None:
            summary["failures"].append(f"{code} {rid}: no exact code row")
            continue
        finished = rec.get("request_finished_at")
        if not isinstance(finished, str) or not re.match(r"^\d{4}-\d{2}-\d{2}", finished):
            summary["failures"].append(f"{code} {rid}: invalid request_finished_at")
            continue
        target = dict(target)
        target["_retrieved_date"] = finished[:10]
        row, _ = normalize_comparison_row(code, target, adapter_sha, identity_sha,
                                          mapping_sha, rid)
        summary["comparison"][code] = {"row": row, "request_id": rid}
    return summary


# ---------------------------------------------------------------------------
# candidate DB
# ---------------------------------------------------------------------------
def _logical_sha(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT code,date,pe_ttm,pb_mrq,ps_ttm,market_cap,total_shares,dividend_yield "
        "FROM stock_valuation ORDER BY code,date").fetchall()
    buf = []
    for r in rows:
        enc = [str(r[0]), str(r[1])]
        for v in r[2:]:
            enc.append("NULL" if v is None else ("ZERO" if v == 0.0 else repr(float(v))))
        buf.append("|".join(enc))
    return sha256_text("\n".join(buf))


def connect_candidate(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


def insert_rows(conn: sqlite3.Connection, rows: list[dict]) -> None:
    for item in rows:
        row = item["row"]
        conn.execute(
            "INSERT OR REPLACE INTO stock_valuation "
            "(code,date,pe_ttm,pb_mrq,ps_ttm,market_cap,total_shares,dividend_yield) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (row["code"], row["date"], row["pe_ttm"], row["pb_mrq"], row["ps_ttm"],
             row["market_cap"], row["total_shares"], row["dividend_yield"]))
        p = item["provenance"]
        conn.execute(
            "INSERT OR REPLACE INTO valuation_provenance "
            "(code,date,request_id,source,source_column_map,raw_row_sha256,"
            "normalized_record_sha256,retrieved_at,adapter_sha256,"
            "identity_snapshot_sha256,mapping_snapshot_sha256,"
            "normalization_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (p["code"], p["date"], p["request_id"], p["source"],
             p["source_column_map"], p["raw_row_sha256"],
             p["normalized_record_sha256"], p["retrieved_at"],
             p["adapter_sha256"], p["identity_snapshot_sha256"],
             p["mapping_snapshot_sha256"], p["normalization_version"]))
    conn.commit()


def insert_errors(conn: sqlite3.Connection, errors: list[dict]) -> None:
    for e in errors:
        conn.execute(
            "INSERT INTO valuation_errors "
            "(code,source,field,error_class,message,raw_value,occurred_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (e.get("code"), e.get("source"), e.get("field"),
             e.get("error_class", "PARSE"), e.get("message"), e.get("raw"), now_iso()))
    conn.commit()


# ---------------------------------------------------------------------------
# checkpoint
# ---------------------------------------------------------------------------
def make_checkpoint(plan: dict, identity_sha: str, mapping_sha: str,
                    builder_sha: str, schema_sha: str, started_at: str,
                    completed_codes: list[str], in_progress_code: str | None,
                    completed_operations: list[str], next_seq: int,
                    counters: dict, source_state: dict,
                    candidate_counters: dict,
                    factual_request_counters: dict | None = None) -> dict:
    cp = {
        "version": "p2_07c_a_checkpoint_v1",
        "plan_sha256": _plan_sha(plan),
        "identity_snapshot_sha256": identity_sha,
        "mapping_snapshot_sha256": mapping_sha,
        "builder_sha256": builder_sha,
        "schema_sha256": schema_sha,
        "started_at": started_at,
        "updated_at": now_iso(),
        "completed_codes": sorted(completed_codes),
        "in_progress_code": in_progress_code,
        "completed_operations": completed_operations,
        "next_request_sequence": next_seq,
        "request_counters": counters,
        "source_state": source_state,
        "candidate_logical_counters": candidate_counters,
    }
    if factual_request_counters is not None:
        cp["request_counters_factual"] = factual_request_counters
    return cp


def counters() -> dict:
    return {"total": 0, "per_source": {}, "duplicate_req_ids": 0}


def compute_factual_request_counters(ledger_records: list[dict]) -> dict:
    """Compute factual request counters from ALL counted_as_real_request
    ledger records. Returns {total, per_source{source: count}}."""
    real = [r for r in ledger_records if r.get("counted_as_real_request")]
    per_source = {}
    for r in real:
        s = r.get("source", "")
        per_source[s] = per_source.get(s, 0) + 1
    return {"total": len(real), "per_source": dict(sorted(per_source.items()))}


def _validate_factual_counters(fc: dict, label: str) -> list[str]:
    """Validate request_counters_factual structure and types.
    Returns list of errors (empty = valid)."""
    errs = []
    if not isinstance(fc, dict):
        return [f"{label}: not an object"]
    total = fc.get("total")
    if isinstance(total, bool) or not isinstance(total, int):
        errs.append(f"{label}.total: must be int, got {type(total).__name__}")
    elif total < 0:
        errs.append(f"{label}.total: negative {total}")
    ps = fc.get("per_source")
    if not isinstance(ps, dict):
        errs.append(f"{label}.per_source: not an object")
    else:
        for s, c in ps.items():
            if isinstance(c, bool) or not isinstance(c, int):
                errs.append(f"{label}.per_source.{s}: must be int, got {type(c).__name__}")
            elif c < 0:
                errs.append(f"{label}.per_source.{s}: negative {c}")
    return errs


def conservation_check(conn: sqlite3.Connection, ledger_records: list[dict],
                       budget: Budget) -> tuple[bool, list[str]]:
    """Stage B lightweight conservation + quick_check (work order §6, §9.1).
    Recomputes from raw ledger + candidate DB; returns (ok, problems)."""
    problems: list[str] = []
    real = [r for r in ledger_records if r.get("counted_as_real_request")]
    ids = [r.get("request_id") for r in real]
    if len(ids) != len(set(ids)):
        problems.append(f"duplicate request_ids in ledger: {len(ids) - len(set(ids))}")
    if budget is not None:
        if len(ids) != budget.total_used:
            problems.append(f"ledger real count {len(ids)} != runtime counter {budget.total_used}")
        per_src = {}
        for r in real:
            per_src[r.get("source", "")] = per_src.get(r.get("source", ""), 0) + 1
        for s, used in budget.per_source_used.items():
            if per_src.get(s, 0) != used:
                problems.append(f"per-source {s}: ledger {per_src.get(s, 0)} != runtime {used}")
    # provenance completeness (recomputed, no double count on duplicate PKs)
    rows = conn.execute("SELECT COUNT(*) FROM stock_valuation").fetchone()[0]
    prov = conn.execute("SELECT COUNT(*) FROM valuation_provenance").fetchone()[0]
    if prov < rows:
        problems.append(f"provenance {prov} < rows {rows}")
    missing = conn.execute(
        "SELECT COUNT(*) FROM valuation_provenance p LEFT JOIN stock_valuation v "
        "ON p.code=v.code AND p.date=v.date WHERE v.code IS NULL").fetchone()[0]
    if missing:
        problems.append(f"{missing} orphan provenance rows")
    # provenance request_id must exist in the JSONL ledger (source of truth)
    ledger_ids = {r.get("request_id") for r in ledger_records if r.get("request_id")}
    orphan = 0
    for (rid,) in conn.execute("SELECT DISTINCT request_id FROM valuation_provenance"):
        if rid and rid not in ledger_ids:
            orphan += 1
    if orphan:
        problems.append(f"{orphan} provenance request_id not in JSONL ledger")
    pkdup = conn.execute(
        "SELECT COUNT(*) FROM (SELECT code,date,COUNT(*) c FROM stock_valuation "
        "GROUP BY code,date HAVING c > 1)").fetchone()[0]
    if pkdup:
        problems.append(f"{pkdup} duplicate primary keys")
    qc = conn.execute("PRAGMA quick_check").fetchone()[0]
    if qc != "ok":
        problems.append(f"quick_check={qc}")
    return len(problems) == 0, problems


def write_progress_snapshot(root: Path, seq: int, code: str, plan_sha: str,
                            identity_sha: str, completed_codes: list[str],
                            budget: Budget, rows: int) -> Path:
    snap_dir = root / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    path = snap_dir / f"progress-{seq:03d}-{code}.json"
    write_json(path, {
        "gate": "G4-progress", "seq": seq, "code": code,
        "recorded_at": now_iso(), "plan_sha256": plan_sha,
        "identity_snapshot_sha256": identity_sha,
        "completed_codes": sorted(completed_codes),
        "completed_count": len(completed_codes),
        "request_counters": budget.snapshot() if budget else {},
        "candidate_rows": rows,
    })
    return path


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------
def create_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="P2-07C-B isolated BJ valuation builder")
    p.add_argument("--identity", type=Path)
    p.add_argument("--official-mapping-evidence", type=Path)
    p.add_argument("--output-db", type=Path)
    p.add_argument("--evidence-root", type=Path, default=Path(APPROVED_ROOT))
    p.add_argument("--checkpoint", type=Path)
    p.add_argument("--max-codes", type=int, default=None,
                   help="attempt cap; None = full identity set (Stage B default)")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--offline-response-cache", type=Path)
    p.add_argument("--dry-run-plan", action="store_true")
    p.add_argument("--max-requests", type=int, default=None)
    p.add_argument("--max-time-minutes", type=int, default=None)
    p.add_argument("--collect-cross-source", action="store_true",
                   help="collect baidu+comparison cross-source for anchors (R1)")
    p.add_argument("--stage-b", action="store_true",
                   help="Stage B mode: full current-active 92xxxx set (explicit "
                        "authorization; Stage A stays capped at 12 and is default)")
    p.add_argument("--stage-a-verdict", type=Path,
                   help="accepted Stage A verdict.json (required for --stage-b)")
    return p


# ---------------------------------------------------------------------------
# main orchestration
# ---------------------------------------------------------------------------
def run_stage_b(argv: list[str] | None = None) -> int:
    """Stage B: full current-active 92xxxx isolated candidate build.

    Explicitly authorized via --stage-b (Stage A remains the default and keeps
    its 12-code cap). Requires an accepted Stage A verdict. Budgets are the
    Stage B hard caps. Every 25 codes a progress snapshot; every 50 codes a
    lightweight conservation + quick_check; any hard gate failure stops the
    build immediately (non-zero).
    """
    args = create_parser().parse_args(argv)
    # M4: attempt cap is a non-negative integer policy; invalid values must be
    # rejected non-zero BEFORE any build. 0 = process no codes this launch.
    if args.max_codes is not None and args.max_codes < 0:
        print("Stage B --max-codes must be a non-negative integer")
        return 1
    evidence_root = args.evidence_root.resolve()
    if not str(evidence_root).startswith(STAGE_B_ROOT_PREFIX):
        print("Stage B evidence-root must start with", STAGE_B_ROOT_PREFIX)
        return 1
    for ar in (APPROVED_ROOT, APPROVED_ROOT_R1):
        if str(evidence_root) == str(Path(ar).resolve()):
            print("Stage B must NOT reuse a Stage A evidence root:", ar)
            return 1
    approved = str(evidence_root)

    build_dir = evidence_root / "build"
    requests_dir = evidence_root / "requests"
    responses_dir = evidence_root / "responses"
    rollback_dir = evidence_root / "rollback"
    for d in (build_dir, requests_dir, responses_dir, rollback_dir):
        d.mkdir(parents=True, exist_ok=True)

    output_db = (args.output_db or (build_dir / "stage-b-bj-valuation.db")).resolve()
    if output_db in (Path(FORMAL_DB).resolve(), Path(SH_SZ_CANDIDATE).resolve()):
        print("refusing to write into pre-existing/production DB", output_db)
        return 1
    if not str(output_db).startswith(approved):
        print("output-db not under evidence root:", output_db)
        return 1
    if output_db.exists() and not args.resume:
        print("candidate db already exists; use --resume to continue it", output_db)
        return 1

    # ---- Stage A sequencing gate (M7): structural, not just the string ----
    verdict_path = args.stage_a_verdict
    va_ok, va_reason = validate_stage_a_verdict(verdict_path)
    if verdict_path is None or not Path(verdict_path).exists():
        print("Stage B requires --stage-a-verdict pointing to an accepted Stage A "
              "verdict.json")
        return 1
    if not va_ok:
        print(f"Stage A verdict rejected (M7 structural gate): {va_reason}")
        return 1

    network_off = os.environ.get(NETWORK_ENV_VAR, "") != "1"
    offline_rebuild = args.offline_response_cache is not None
    cache_dir = None
    if offline_rebuild:
        network_off = True
        cache_dir = args.offline_response_cache.resolve()

    # ---- dynamic identity (recomputed, not a hard-coded 324) ----
    if args.identity and args.identity.exists():
        doc = json.loads(args.identity.read_text(encoding="utf-8"))
        securities = [
            Security(code=s["code"], name=s.get("name", ""), market=s.get("market", ""),
                     is_active=int(s.get("is_active", 0)), list_date=s.get("list_date", ""),
                     mapping=s.get("mapping", {}))
            for s in doc.get("securities", [])
        ]
    else:
        securities = load_all_active_bj_securities()
    valid, errs = validate_identity_stage_b(securities, STAGE_B_IDENTITY_MAX)
    if not valid:
        print("identity invalid:", "; ".join(errs))
        return 1
    identity_sha = identity_snapshot_sha(securities)
    write_stage_b_identity(evidence_root, securities, identity_sha, STAGE_B_IDENTITY_SQL)

    mapping = dict(OFFICIAL_MAPPING_ANCHORS)
    if args.official_mapping_evidence and args.official_mapping_evidence.exists():
        mapping = read_official_mapping(args.official_mapping_evidence)
    mapping_sha = mapping_snapshot_sha(mapping)

    plan = build_plan_stage_b(securities, identity_sha)

    if args.dry_run_plan:
        plan_dir = evidence_root / "plan"
        plan_dir.mkdir(parents=True, exist_ok=True)
        write_json(plan_dir / "stage-b-plan.json", plan)
        write_json(plan_dir / "stage-b-plan-dry.json",
                   {"plan": plan, "identity_sha": identity_sha,
                    "mapping_sha": mapping_sha, "network": (not network_off),
                    "stage_a_verdict": str(verdict_path)})
        print(json.dumps({"plan_codes": plan["plan_codes"], "total_codes": plan["total_codes"],
                          "identity_sha": identity_sha, "mapping_sha": mapping_sha,
                          "network": (not network_off)}, ensure_ascii=False, indent=2))
        return 0

    builder_sha = sha256_file(Path(__file__).resolve())
    schema_sha = sha256_text(SCHEMA_SQL)

    checkpoint_path = (args.checkpoint or (build_dir / "checkpoint.json")).resolve()
    if not str(checkpoint_path).startswith(approved):
        print("checkpoint path not under evidence root")
        return 1

    completed_codes: list[str] = []
    resume_cp: dict | None = None
    if args.resume and checkpoint_path.exists():
        resume_cp = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        cp = resume_cp
        if cp.get("version") != STAGE_B_CHECKPOINT_VERSION or cp.get("stage") != "B":
            print(f"checkpoint version/stage mismatch: {cp.get('version')}/{cp.get('stage')}")
            return 3
        expected = {
            "plan_sha256": _plan_sha(plan),
            "identity_snapshot_sha256": identity_sha,
            "mapping_snapshot_sha256": mapping_sha,
            "builder_sha256": builder_sha,
            "schema_sha256": schema_sha,
        }
        for k, v in expected.items():
            if cp.get(k) != v:
                print(f"checkpoint {k} mismatch {cp.get(k)} != {v}")
                return 3
        completed_codes = list(cp.get("completed_codes", []))

        # M3: reuse the frozen plan (never re-sample on resume). The persisted
        # plan's stable fields and identity SHA must match the frozen one used
        # to build the checkpoint; otherwise the baseline has drifted and we
        # must stop (BLOCKED_BASELINE_DRIFT), not silently re-sample.
        persisted = evidence_root / "plan" / "stage-b-plan.json"
        if persisted.exists():
            saved_plan = json.loads(persisted.read_text(encoding="utf-8"))
        else:
            saved_plan = None
        if saved_plan is None or _plan_sha(saved_plan) != cp.get("plan_sha256"):
            print("BLOCKED_BASELINE_DRIFT: no stable persisted plan matching the "
                  "checkpoint (identity/anchor/sample drifted); refusing to re-sample")
            return 3
        if saved_plan.get("identity_snapshot_sha256") != identity_sha:
            print("BLOCKED_BASELINE_DRIFT: persisted plan identity SHA "
                  f"{saved_plan.get('identity_snapshot_sha256')} != current "
                  f"{identity_sha}; refusing to re-sample")
            return 3
        # Continue the build against the frozen plan, not a freshly re-sampled one.
        plan = saved_plan
    elif args.resume:
        print("resume requested but no checkpoint exists")
        return 3

    # The exact plan used by a real build is evidence payload, not a dry-run
    # convenience. Persist it before opening candidate state; resume retains
    # and reuses the already SHA-validated copy above.
    plan_path = evidence_root / "plan" / "stage-b-plan.json"
    if not args.resume:
        if plan_path.exists():
            print("BLOCKED_STAGE_B_PLAN: new run refuses to overwrite an existing plan")
            return 1
        try:
            atomic_write_json_no_clobber(plan_path, plan)
        except OSError as exc:
            print(f"BLOCKED_STAGE_B_PLAN: cannot persist frozen plan: {exc}")
            return 1

    if offline_rebuild and cache_dir is not None:
        # The cache remains an external read-only input, but its ledger is a
        # provenance payload and must be materialized byte-for-byte into this
        # evidence root before any candidate DB or budget state is opened.
        cache_dir = cache_dir.resolve()
        if not cache_dir.is_dir():
            print("BLOCKED_OFFLINE_CACHE: specified --offline-response-cache "
                  "directory does not exist:", cache_dir)
            return 1
        offline_ledger_path = cache_dir / "request-ledger.jsonl"
        if not offline_ledger_path.is_file():
            print("BLOCKED_OFFLINE_CACHE: specified --offline-response-cache has "
                  "no request-ledger.jsonl:", offline_ledger_path)
            return 1
        source_ledger = RequestLedger(offline_ledger_path)
        if source_ledger.parse_errors:
            print("BLOCKED_REQUEST_LEDGER: " + "; ".join(source_ledger.parse_errors))
            return 3 if args.resume else 1
        source_id_errors = validate_ledger_request_ids(source_ledger.records())
        if source_id_errors:
            print("BLOCKED_REQUEST_ID: " + "; ".join(source_id_errors))
            return 3 if args.resume else 1
        materialized_ledger_path = requests_dir / "request-ledger.jsonl"
        source_sha = sha256_file(offline_ledger_path)
        if args.resume:
            if not materialized_ledger_path.is_file():
                print("BLOCKED_OFFLINE_LEDGER: resume root has no materialized ledger")
                return 3
            if sha256_file(materialized_ledger_path) != source_sha:
                print("BLOCKED_OFFLINE_LEDGER: external ledger differs from the "
                      "materialized resume ledger")
                return 3
        else:
            if materialized_ledger_path.exists():
                print("BLOCKED_OFFLINE_LEDGER: new run refuses to overwrite an "
                      "existing materialized ledger")
                return 1
            try:
                copied_sha = atomic_copy_verified(offline_ledger_path,
                                                  materialized_ledger_path)
            except OSError as exc:
                print(f"BLOCKED_OFFLINE_LEDGER: materialization failed: {exc}")
                return 1
            if copied_sha != source_sha:
                print("BLOCKED_OFFLINE_LEDGER: source/target SHA mismatch")
                return 1
        ledger = RequestLedger(materialized_ledger_path)
        cache = ResponseCache(cache_dir)
    else:
        ledger = RequestLedger(requests_dir / "request-ledger.jsonl")
        cache = ResponseCache(responses_dir)

    # Every Stage B ledger is validated before state open. Offline source and
    # materialized ledgers therefore share the same fail-closed contract.
    if ledger.parse_errors:
        print("BLOCKED_REQUEST_LEDGER: " + "; ".join(ledger.parse_errors))
        return 3 if args.resume else 1
    ledger_id_errors = validate_ledger_request_ids(ledger.records())
    if ledger_id_errors:
        print("BLOCKED_REQUEST_ID: " + "; ".join(ledger_id_errors))
        return 3 if args.resume else 1

    g0_ok, g0_reason = ensure_stage_b_g0(evidence_root, args.resume)
    if not g0_ok:
        print("BLOCKED_G0: " + g0_reason)
        return 3 if args.resume else 1

    conn = connect_candidate(output_db)
    budget = make_stage_b_budget(wallclock_min=args.max_time_minutes or STAGE_B_WALL_CLOCK_MAX_MIN)
    if args.resume and resume_cp is not None:
        try:
            rc = resume_cp.get("request_counters") or {}
            budget.restore_snapshot(rc)
            remaining = budget.restored_deadline_remaining()
            if remaining is not None:
                budget.set_deadline_remaining(remaining)
        except (ValueError, TypeError) as e:
            print(f"BLOCKED_INVALID_BUDGET_SNAPSHOT: {e}; refusing to zero the budget")
            conn.close()
            return 3
    ak = import_akshare()

    started_at = now_iso()
    completed_ops: list[str] = []
    next_seq = len(ledger.records())
    completed_set = set(completed_codes)
    factual = compute_factual_request_counters(ledger.records())
    if args.resume and resume_cp is not None:
        fc = resume_cp.get("request_counters_factual")
        if fc is None:
            print("BLOCKED_FACTUAL_COUNTERS: checkpoint missing request_counters_factual; "
                  "refusing resume")
            conn.close()
            return 3
        errs = _validate_factual_counters(fc, "checkpoint")
        if errs:
            print(f"BLOCKED_FACTUAL_COUNTERS: {'; '.join(errs)}")
            conn.close()
            return 3
        if fc.get("total") != factual.get("total"):
            print(f"BLOCKED_FACTUAL_COUNTERS: total mismatch cp={fc.get('total')} "
                  f"ledger={factual.get('total')}")
            conn.close()
            return 3
        for s, c in fc.get("per_source", {}).items():
            if factual.get("per_source", {}).get(s, 0) != c:
                print(f"BLOCKED_FACTUAL_COUNTERS: per-source {s} cp={c} "
                      f"ledger={factual.get('per_source', {}).get(s, 0)}")
                conn.close()
                return 3
        for s, c in factual.get("per_source", {}).items():
            if s not in fc.get("per_source", {}):
                print(f"BLOCKED_FACTUAL_COUNTERS: ledger source {s} ({c}) "
                      f"missing from checkpoint")
                conn.close()
                return 3
    budget_warned = False
    analysis_dir = evidence_root / "analysis"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    # M4: attempt cap counts only freshly processed codes THIS launch. Codes
    # already completed in a prior run (resume) never count toward the new cap.
    attempts_this_run = 0
    max_attempts = args.max_codes  # None => full identity set

    for code in plan["plan_codes"]:
        if code in completed_set:
            continue
        if max_attempts is not None and attempts_this_run >= max_attempts:
            log.info("Stage B attempt cap reached (%s); stopping", max_attempts)
            break
        attempts_this_run += 1
        if time.monotonic() >= budget.deadline:
            log.warning("wall clock budget reached; stopping")
            break
        if (not budget_warned and budget.total_cap
                and budget.total_used >= budget.total_cap * STAGE_B_BUDGET_WARN_PCT):
            write_json(analysis_dir / "budget-warning.json",
                       {"gate": "G5-warning", "recorded_at": now_iso(),
                        "pct_used": round(budget.total_used / budget.total_cap, 4),
                        **budget.snapshot()})
            budget_warned = True

        rid = f"req-{code}-{next_seq:05d}"
        next_seq += 1

        if offline_rebuild and cache_dir:
            # R3-4: select ALL PROVEN ledger records for this code/source FIRST
            # (never filter by cache existence). Then validate each record's
            # request_id, response_sha256, cache exists, gzip/JSON, canonical SHA.
            if not budget.try_allow("stock_value_em", 1):
                log.warning("budget exhausted before %s", code)
                break
            ledger_recs = ledger.records()
            replay = [r for r in ledger_recs
                      if r.get("code_or_batch_scope") == code
                      and r.get("source") == "stock_value_em"
                      and r.get("outcome") == STATUS_PROVEN]
            if not replay:
                # no PROVEN ledger record to replay is an explicit hard failure
                write_json(analysis_dir / "offline-cache-missing.json", {
                    "gate": "G7-offline-cache-missing", "recorded_at": now_iso(),
                    "code": code, "plan_sha256": _plan_sha(plan),
                    "cache_dir": str(cache_dir),
                    "detail": "no PROVEN ledger record to replay for code",
                })
                print(f"BLOCKED_OFFLINE_CACHE: no PROVEN ledger record for {code} "
                      f"in {cache_dir}; refusing to treat 'no record' as success")
                conn.close()
                return 1
            records = []
            for rec in replay:
                rid = rec.get("request_id")
                if not _request_id_ok(rid):
                    write_json(analysis_dir / "offline-cache-invalid-request-id.json", {
                        "gate": "G7-offline-cache-invalid-request-id", "recorded_at": now_iso(),
                        "code": code, "request_id": repr(rid), "reason": "invalid request_id",
                        "cache_dir": str(cache_dir),
                    })
                    print(f"BLOCKED_OFFLINE_CACHE: invalid request_id {rid!r} for {code}")
                    conn.close()
                    return 1
                ok, reason = validate_cache_sha(ledger_recs, cache_dir, rid)
                if not ok:
                    write_json(analysis_dir / "offline-cache-sha-mismatch.json", {
                        "gate": "G7-offline-cache-sha-mismatch", "recorded_at": now_iso(),
                        "code": code, "request_id": rid, "reason": reason,
                        "cache_dir": str(cache_dir),
                    })
                    print(f"BLOCKED_OFFLINE_CACHE: {reason}")
                    conn.close()
                    return 1
                records.extend(cache.load(rid))
            df = _RecordsDF(records)
            rows, errors = normalize_value_em(
                code, df, builder_sha, identity_sha, mapping_sha, replay[0]["request_id"],
                STAGE_B_NORMALIZATION_VERSION)
            insert_rows(conn, rows)
            insert_errors(conn, errors)
            budget.consume("stock_value_em", 1, ok=True)
            completed_ops.append(f"offline:{code}")
            completed_set.add(code)
            cp = make_checkpoint_stage_b(
                plan, identity_sha, mapping_sha, builder_sha, schema_sha, started_at,
                sorted(completed_set), code, completed_ops, next_seq,
                budget.snapshot(), budget.snapshot(),
                {"rows": conn.execute("SELECT COUNT(*) FROM stock_valuation").fetchone()[0]},
                factual_request_counters=factual)
            atomic_write_json(checkpoint_path, cp)
            _n = len(completed_set)
            if _n % STAGE_B_PROGRESS_SNAPSHOT_EVERY == 0:
                _rows = conn.execute("SELECT COUNT(*) FROM stock_valuation").fetchone()[0]
                write_progress_snapshot(evidence_root, _n // STAGE_B_PROGRESS_SNAPSHOT_EVERY,
                                        code, _plan_sha(plan), identity_sha,
                                        sorted(completed_set), budget, _rows)
            if _n % STAGE_B_CONSERVATION_EVERY == 0:
                write_offline_conservation_snapshot(
                    evidence_root, conn, ledger.records(), completed_set, code,
                    _n // STAGE_B_CONSERVATION_EVERY)
            continue

        # network build of historical main source (single concurrency)
        if network_off:
            log.warning("network is OFF; cannot build %s (set %s=1)", code, NETWORK_ENV_VAR)
            break
        if not budget.try_allow("stock_value_em", 1):
            log.warning("budget exhausted before %s", code)
            break
        df, err, attempt, is_retry, latency, status = _call_with_retry(
            ak, "stock_value_em", {"code": code}, budget)
        budget.consume("stock_value_em", 1, ok=(status == STATUS_PROVEN))
        # R1-1: SHA must match the actual canonical cache content. Compute
        # from the cache BEFORE writing the ledger entry.
        if df is not None and not getattr(df, "empty", False) and err is None:
            recs = df_to_records(df)
            sha = cache.save(rid, recs)
        else:
            sha = _df_to_sha(df)
            recs = []
        ledger.append({
            "request_id": rid, "source": "stock_value_em",
            "endpoint": "akshare.stock_value_em", "code_or_batch_scope": code,
            "request_started_at": started_at, "request_finished_at": now_iso(),
            "attempt_no": attempt, "is_retry": int(is_retry),
            "outcome": status, "latency_ms": round(latency, 3),
            "response_sha256": sha, "error_class": "" if err is None else classify_str(err),
            "error": err or "", "counted_as_real_request": 1,
        })
        if err is not None:
            log.warning("history fetch failed for %s: %s", code, err)
            insert_errors(conn, [{"code": code, "date": "", "source": "stock_value_em",
                                  "field": "", "error_class": status,
                                  "message": err, "raw": ""}])
            completed_ops.append(f"failed:{code}:{status}")
            completed_set.add(code)
            _stage_b_checkpoint_and_snap(
                evidence_root, conn, ledger, budget, plan, identity_sha, mapping_sha,
                builder_sha, schema_sha, started_at, checkpoint_path, completed_set,
                code, completed_ops, next_seq)
            continue
        if df is None or getattr(df, "empty", False):
            insert_errors(conn, [{"code": code, "date": "", "source": "stock_value_em",
                                  "field": "", "error_class": "CODE_NOT_FOUND",
                                  "message": "empty historical response", "raw": ""}])
            log.warning("empty response for %s", code)
            completed_ops.append(f"empty:{code}:CODE_NOT_FOUND")
            completed_set.add(code)
            _stage_b_checkpoint_and_snap(
                evidence_root, conn, ledger, budget, plan, identity_sha, mapping_sha,
                builder_sha, schema_sha, started_at, checkpoint_path, completed_set,
                code, completed_ops, next_seq)
            continue
        rows, errors = normalize_value_em(code, df, builder_sha, identity_sha,
                                          mapping_sha, rid,
                                          STAGE_B_NORMALIZATION_VERSION)
        insert_rows(conn, rows)
        insert_errors(conn, errors)
        completed_ops.append(f"history:{code}")
        completed_set.add(code)
        _stage_b_checkpoint_and_snap(
            evidence_root, conn, ledger, budget, plan, identity_sha, mapping_sha,
            builder_sha, schema_sha, started_at, checkpoint_path, completed_set,
            code, completed_ops, next_seq)

    # ---- Stage B cross-source collection (frozen plan sample; never re-sample) ----
    cs = {}
    sample = list(plan.get("cross_source_sample_codes", []))
    if offline_rebuild and cache_dir is not None:
        cs = rebuild_cross_source_from_cache(
            ledger.records(), cache_dir, sample, builder_sha, identity_sha, mapping_sha)
        if cs.get("failures"):
            print("BLOCKED_OFFLINE_CROSS_SOURCE: " + "; ".join(cs["failures"][:10]))
            conn.close()
            return 1
        log.info("Stage B offline cross-source: baidu=%s comparison=%s",
                 len(cs.get("baidu", {})), len(cs.get("comparison", {})))
    elif not network_off:
        cs = collect_cross_source(
            ak, budget, ledger, cache, sample,
            builder_sha, identity_sha, mapping_sha, network_off)
        log.info("Stage B cross-source: baidu=%s comparison=%s failures=%d",
                 len(cs.get("baidu", {})), len(cs.get("comparison", {})),
                 len(cs.get("failures", [])))
    cs["required_anchor_codes"] = sorted(plan.get("required_anchor_codes", []))
    cs["out_of_scope_anchor_codes"] = sorted(plan.get("out_of_scope_anchor_codes", []))
    cs["sample_codes"] = sorted(plan.get("cross_source_sample_codes", []))
    cs["sample_identity_sha256"] = plan.get("identity_snapshot_sha256")
    cs["plan_sha256"] = _plan_sha(plan)
    write_json(analysis_dir / "cross-source-collection.json", cs)

    # ---- M1: out-of-scope anchors must NOT be silently dropped ----
    out_of_scope = sorted(plan.get("out_of_scope_anchor_codes", []))
    if out_of_scope:
        write_json(analysis_dir / "out-of-scope-anchors.json", {
            "gate": "G6-out-of-scope-anchors", "recorded_at": now_iso(),
            "out_of_scope_anchors": out_of_scope,
            "reason": ("anchor not present in current active BJ identity; "
                       "current formal market may differ (e.g. SZ/SH)"),
            "requires_nova_adjudication": True,
            "required_anchor_codes": sorted(plan.get("required_anchor_codes", [])),
            "sample_identity_sha256": identity_sha,
            "plan_sha256": _plan_sha(plan),
        })
        log.warning("Stage B out-of-scope anchors recorded for Nova adjudication: %s",
                    out_of_scope)

    # ---- R3-5: honest + conserved cache-sha migration report (offline source) ----
    # Production call site for the R2->R3 offline migration path. The offline
    # cache dir is the source of truth for canonical response payloads; its
    # PROVEN ledger drives the report. We materialize each PROVEN cache into
    # this evidence root's responses/ mirror, then generate analysis/
    # cache-sha-migration.json. Never hand-fabricated; extraction is copy-only
    # from the R2 source root. Non-canonical/missing entries become "failed"
    # records, which the auditor hard-fails (merge not ready).
    if offline_rebuild and cache_dir is not None:
        proven = [r for r in ledger.records()
                  if r.get("outcome") == STATUS_PROVEN
                  and _request_id_ok(r.get("request_id"))]
        for rec in proven:
            rid = rec["request_id"]
            src_fp = Path(cache_dir) / f"{rid}.canonical.json.gz"
            dst_fp = responses_dir / f"{rid}.canonical.json.gz"
            if src_fp.exists() and not dst_fp.exists():
                import shutil
                shutil.copy2(src_fp, dst_fp)
        if proven:
            mig = write_cache_sha_migration_report(
                evidence_root, proven, responses_dir, str(cache_dir.resolve()))
            log.info("R3-5 migration report: processed=%s converted=%s "
                     "unchanged=%s failed=%s",
                     mig["processed_records"], mig["converted_records"],
                     mig["unchanged_records"], mig["failed_records"])

    conn.close()
    return 0


def write_offline_conservation_snapshot(evidence_root: Path,
                                        conn: sqlite3.Connection,
                                        ledger_records: list[dict],
                                        completed_set: set[str], code: str,
                                        seq: int) -> Path:
    """Record fail-closed offline replay conservation from factual inputs."""
    problems = validate_ledger_request_ids(ledger_records)
    factual = compute_factual_request_counters(ledger_records)
    if sum(factual["per_source"].values()) != factual["total"]:
        problems.append("factual per-source counters do not conserve to total")
    rows = conn.execute("SELECT COUNT(*) FROM stock_valuation").fetchone()[0]
    provenance = conn.execute("SELECT COUNT(*) FROM valuation_provenance").fetchone()[0]
    if provenance != rows:
        problems.append(f"provenance {provenance} != rows {rows}")
    outside = [row[0] for row in conn.execute(
        "SELECT DISTINCT code FROM stock_valuation ORDER BY code")
        if row[0] not in completed_set]
    if outside:
        problems.append(f"candidate codes outside completed set: {outside[:5]}")
    quick_check = conn.execute("PRAGMA quick_check").fetchone()[0]
    if quick_check != "ok":
        problems.append(f"quick_check={quick_check}")
    artifact = {
        "gate": "G3-offline-conservation", "seq": seq,
        "completed_count": len(completed_set), "code": code,
        "recorded_at": now_iso(), "ok": not problems,
        "problems": problems, "factual_request_counters": factual,
        "candidate_rows": rows, "provenance_rows": provenance,
        "quick_check": quick_check,
    }
    out_dir = evidence_root / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"conservation-{seq:02d}.json"
    atomic_write_json_no_clobber(path, artifact)
    if problems:
        raise SystemExit(4)
    return path


def _stage_b_checkpoint_and_snap(evidence_root, conn, ledger, budget, plan,
                                 identity_sha, mapping_sha, builder_sha, schema_sha,
                                 started_at, checkpoint_path, completed_set, code,
                                 completed_ops, next_seq) -> None:
    factual = compute_factual_request_counters(ledger.records())
    cp = make_checkpoint_stage_b(
        plan, identity_sha, mapping_sha, builder_sha, schema_sha, started_at,
        sorted(completed_set), code, completed_ops, next_seq,
        budget.snapshot(), budget.snapshot(),
        {"rows": conn.execute("SELECT COUNT(*) FROM stock_valuation").fetchone()[0]},
        factual_request_counters=factual)
    atomic_write_json(checkpoint_path, cp)
    _stage_b_checks(evidence_root, conn, ledger, budget, completed_set, code,
                    plan, identity_sha)


def _stage_b_checks(evidence_root, conn, ledger, budget, completed_set, code,
                    plan, identity_sha, records=None) -> None:
    """Progress snapshot every 25; conservation + quick_check every 50. Any
    conservation failure raises SystemExit(4) to stop the build immediately."""
    n = len(completed_set)
    if n % STAGE_B_PROGRESS_SNAPSHOT_EVERY == 0:
        rows = conn.execute("SELECT COUNT(*) FROM stock_valuation").fetchone()[0]
        write_progress_snapshot(evidence_root, n // STAGE_B_PROGRESS_SNAPSHOT_EVERY,
                                code, _plan_sha(plan), identity_sha,
                                sorted(completed_set), budget, rows)
    if n % STAGE_B_CONSERVATION_EVERY == 0:
        recs = records if records is not None else ledger.records()
        ok, problems = conservation_check(conn, recs, budget)
        out_dir = evidence_root / "analysis"
        out_dir.mkdir(parents=True, exist_ok=True)
        write_json(out_dir / f"conservation-{n // STAGE_B_CONSERVATION_EVERY:02d}.json", {
            "gate": "G3-conservation", "seq": n // STAGE_B_CONSERVATION_EVERY,
            "completed_count": n, "code": code, "recorded_at": now_iso(),
            "ok": ok, "problems": problems, "request_counters": budget.snapshot()})
        if not ok:
            print("CONSERVATION HARD GATE FAILED:", "; ".join(problems))
            raise SystemExit(4)


def run(argv: list[str] | None = None) -> int:
    args = create_parser().parse_args(argv)
    if getattr(args, "stage_b", False):
        return run_stage_b(argv)
    evidence_root = args.evidence_root.resolve()
    approved_roots = [APPROVED_ROOT, APPROVED_ROOT_R1]
    if not any(
        str(evidence_root) == ar or str(evidence_root).startswith(ar + os.sep)
        for ar in approved_roots
    ):
        log.error("evidence-root not under approved roots (%s)",
                  ", ".join(approved_roots))
        return 1
    approved = str(evidence_root)

    build_dir = evidence_root / "build"
    requests_dir = evidence_root / "requests"
    responses_dir = evidence_root / "responses"
    rollback_dir = evidence_root / "rollback"
    for d in (build_dir, requests_dir, responses_dir, rollback_dir):
        d.mkdir(parents=True, exist_ok=True)

    output_db = (args.output_db or (build_dir / "stage-a-bj-valuation.db")).resolve()
    if output_db in (Path(FORMAL_DB).resolve(), Path(SH_SZ_CANDIDATE).resolve()):
        print("refusing to write into pre-existing/production DB", output_db)
        return 1
    if not str(output_db).startswith(approved):
        print("output-db not under evidence root:", output_db)
        return 1

    network_off = os.environ.get(NETWORK_ENV_VAR, "") != "1"
    offline_rebuild = args.offline_response_cache is not None
    cache_dir = None
    if offline_rebuild:
        network_off = True
        cache_dir = args.offline_response_cache.resolve()

    # ---- identity ---- #
    if args.identity and args.identity.exists():
        doc = json.loads(args.identity.read_text(encoding="utf-8"))
        securities = [
            Security(code=s["code"], name=s.get("name", ""), market=s.get("market", ""),
                     is_active=int(s.get("is_active", 0)), list_date=s.get("list_date", ""),
                     mapping=s.get("mapping", {}))
            for s in doc.get("securities", [])
        ]
    else:
        securities = security_list_from_formal(APPROVED_CODES)
    valid, errs = validate_identity(securities, APPROVED_CODES, STAGE_A_CODES_LIMIT)
    if not valid:
        print("identity invalid:", "; ".join(errs))
        return 1
    identity_sha = identity_snapshot_sha(securities)

    mapping = dict(OFFICIAL_MAPPING_ANCHORS)
    if args.official_mapping_evidence and args.official_mapping_evidence.exists():
        mapping = read_official_mapping(args.official_mapping_evidence)
    mapping_sha = mapping_snapshot_sha(mapping)

    plan = build_plan(securities, args.max_codes)

    if args.dry_run_plan:
        plan_dir = evidence_root / "plan"
        plan_dir.mkdir(parents=True, exist_ok=True)
        write_json(plan_dir / "stage-a-plan.json", plan)
        write_json(plan_dir / "stage-a-plan-dry.json",
                   {"plan": plan, "identity_sha": identity_sha,
                    "mapping_sha": mapping_sha, "network": (not network_off)})
        print(json.dumps({"plan_codes": plan["plan_codes"], "identity_sha": identity_sha,
                          "mapping_sha": mapping_sha, "network": (not network_off)},
                         ensure_ascii=False, indent=2))
        return 0

    builder_sha = sha256_file(Path(__file__).resolve())
    schema_sha = sha256_text(SCHEMA_SQL)

    checkpoint_path = (args.checkpoint or (build_dir / "checkpoint.json")).resolve()
    if not str(checkpoint_path).startswith(approved):
        print("checkpoint path not under evidence root")
        return 1

    completed_codes: list[str] = []
    if args.resume and checkpoint_path.exists():
        cp = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        expected = {
"plan_sha256": _plan_sha(plan),
            "identity_snapshot_sha256": identity_sha,
            "mapping_snapshot_sha256": mapping_sha,
            "builder_sha256": builder_sha,
            "schema_sha256": schema_sha,
        }
        for k, v in expected.items():
            if cp.get(k) != v:
                print(f"checkpoint {k} mismatch {cp.get(k)} != {v}")
                return 3
        completed_codes = list(cp.get("completed_codes", []))
    elif args.resume:
        print("resume requested but no checkpoint exists")
        return 3

    conn = connect_candidate(output_db)
    ledger = RequestLedger(requests_dir / "request-ledger.jsonl")
    cache = ResponseCache(responses_dir)
    budget = Budget(total_cap=args.max_requests or TOTAL_REAL_CAP,
                    wallclock_min=args.max_time_minutes or WALL_CLOCK_MAX_MIN)
    ak = import_akshare()

    started_at = now_iso()
    completed_ops: list[str] = []
    next_seq = len(ledger.records())
    attempted = 0
    attempt_cap = args.max_codes or STAGE_A_CODES_LIMIT

    for code in plan["plan_codes"]:
        if code in completed_codes:
            continue
        if attempt_cap is not None and attempted >= attempt_cap:
            log.info("reached attempt cap %d; controlled stop (Run A1/A2 semantics)",
                     attempt_cap)
            break
        if time.monotonic() >= budget.deadline:
            log.warning("wall clock budget reached; stopping")
            break

        attempted += 1
        rid = f"req-{code}-{next_seq:05d}"
        next_seq += 1

        if offline_rebuild and cache_dir:
            # R3-4: select ALL PROVEN ledger records for this code/source FIRST
            # (never filter by cache existence). Then validate each record's
            # request_id, response_sha256, cache exists, gzip/JSON, canonical SHA.
            ledger_recs = ledger.records()
            replay = [r for r in ledger_recs
                      if r.get("code_or_batch_scope") == code
                      and r.get("source") == "stock_value_em"
                      and r.get("outcome") == STATUS_PROVEN]
            if not replay:
                # no PROVEN ledger record to replay is an explicit hard failure
                print(f"BLOCKED_OFFLINE_CACHE: no PROVEN ledger record for {code} "
                      f"in {cache_dir}; refusing to treat 'no record' as success")
                conn.close()
                return 1
            records = []
            for rec in replay:
                rid = rec.get("request_id")
                if not _request_id_ok(rid):
                    print(f"BLOCKED_OFFLINE_CACHE: invalid request_id {rid!r} for {code}")
                    conn.close()
                    return 1
                ok, reason = validate_cache_sha(ledger_recs, cache_dir, rid)
                if not ok:
                    print(f"BLOCKED_OFFLINE_CACHE: {reason}")
                    conn.close()
                    return 1
                records.extend(cache.load(rid))
            df = _RecordsDF(records)
            rows, errors = normalize_value_em(code, df, builder_sha, identity_sha,
                                              mapping_sha, replay[0]["request_id"])
            insert_rows(conn, rows)
            insert_errors(conn, errors)
            completed_ops.append(f"offline:{code}")
            completed_codes.append(code)

            cp = make_checkpoint(
                plan, identity_sha, mapping_sha, builder_sha, schema_sha, started_at,
                completed_codes, code, completed_ops, next_seq,
                budget.snapshot(), budget.snapshot(),
                {"rows": conn.execute("SELECT COUNT(*) FROM stock_valuation").fetchone()[0]},
            )
            atomic_write_json(checkpoint_path, cp)
            continue

        # network build of historical main source
        if network_off:
            log.warning("network is OFF; cannot build %s (set %s=1)", code, NETWORK_ENV_VAR)
            break
        if not budget.try_allow("stock_value_em", 1):
            log.warning("budget exhausted before %s", code)
            break
        df, err, attempt, is_retry, latency, status = _call_with_retry(
            ak, "stock_value_em", {"code": code}, budget)
        budget.consume("stock_value_em", 1, ok=(status == STATUS_PROVEN))
        if df is not None and not getattr(df, "empty", False) and err is None:
            recs = df_to_records(df)
            sha = cache.save(code, recs)
        else:
            sha = _df_to_sha(df)
            recs = []
        ledger.append({
            "request_id": code, "source": "stock_value_em",
            "endpoint": "akshare.stock_value_em", "code_or_batch_scope": code,
            "request_started_at": started_at, "request_finished_at": now_iso(),
            "attempt_no": attempt, "is_retry": int(is_retry),
            "outcome": status, "latency_ms": round(latency, 3),
            "response_sha256": sha, "error_class": "" if err is None else classify_str(err),
            "error": err or "", "counted_as_real_request": 1,
        })
        if err is not None:
            log.warning("history fetch failed for %s: %s", code, err)
            insert_errors(conn, [{"code": code, "date": "", "source": "stock_value_em",
                                  "field": "", "error_class": classify_str(err, ""),
                                  "message": err, "raw": ""}])
            completed_codes.append(code)
            continue
        if df is None or getattr(df, "empty", False):
            insert_errors(conn, [{"code": code, "date": "", "source": "stock_value_em",
                                  "field": "", "error_class": "EMPTY",
                                  "message": "empty historical response", "raw": ""}])
            log.warning("empty response for %s", code)
            continue
        rows, errors = normalize_value_em(code, df, builder_sha, identity_sha,
                                          mapping_sha, code)
        insert_rows(conn, rows)
        insert_errors(conn, errors)
        completed_ops.append(f"history:{code}")
        completed_codes.append(code)

        # per-code small-commit checkpoint (atomic replace)
        cp = make_checkpoint(
            plan, identity_sha, mapping_sha, builder_sha, schema_sha, started_at,
            completed_codes, code, completed_ops, next_seq,
            budget.snapshot(), budget.snapshot(),
            {"rows": conn.execute("SELECT COUNT(*) FROM stock_valuation").fetchone()[0]},
        )
        atomic_write_json(checkpoint_path, cp)

    # ---- R1 cross-source collection (explicit flag; audit-only) ----
    cs = {}
    if not network_off and not offline_rebuild and args.collect_cross_source:
        cs = collect_cross_source(
            ak, budget, ledger, cache, list(CROSS_SOURCE_ANCHORS),
            builder_sha, identity_sha, mapping_sha, network_off)
        analysis_dir = evidence_root / "analysis"
        analysis_dir.mkdir(parents=True, exist_ok=True)
        write_json(analysis_dir / "cross-source-collection.json", cs)
        log.info("cross-source collected: baidu=%s comparison=%s failures=%d",
                 len(cs.get("baidu", {})), len(cs.get("comparison", {})),
                 len(cs.get("failures", [])))

    conn.close()
    return 0


# ---------------------------------------------------------------------------
# helper stubs (kept near usage)
# ---------------------------------------------------------------------------
VALID_NULL = {None, ""}


class _RecordsDF:
    """Minimal records-like wrapper mimicking dataframe surface used by build."""

    def __init__(self, records: list[dict]):
        self._records = list(records)

    def to_record_list(self):
        return list(self._records)

    def to_dict(self, orient="records"):
        return list(self._records)

    def to_list(self):
        return list(self._records)


if __name__ == "__main__":
    sys.exit(run())