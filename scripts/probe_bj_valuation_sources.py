#!/usr/bin/env python3
"""P2-07B: Probe BJ-stock valuation sources for QuantStar.

Phase 1 (local, always): query stock_info -> build old<->new code mapping with
  the P2-07B mapping schema (§4.1): old_code / new_code / name_old / name_new /
  mapping_status / confidence / evidence_* / conflicts / usable_for_probe /
  usable_for_migration / reason. mapping_status is one of
  OFFICIAL_PROVEN / CANDIDATE_NAME_MATCH / UNPAIRED_OLD / UNPAIRED_NEW /
  CONFLICT / UNRESOLVED.

Phase 2 (network, --probe + NETWORK_PROBE_ENABLED=1): bounded real probe of BJ valuation sources.
  Hard limits: unique codes <= 12, sources <= 4, total real requests <= 80,
  per-source requests <= 30, wall clock <= 20 minutes, source fusing after 3
  consecutive failures, retry at most 1 per request (network/timeout only).

P2-07B fixes over B4-R1:
  1. Observation schema (§5.3): source / request_id / code_requested /
     code_resolved / field / value / unit / as_of_date / period_start /
     period_end / is_historical_series / source_column /
     source_title_or_endpoint / response_sha256 / retrieved_at / status /
     error_class / latency_ms.
  2. RATE_LIMITED added as a distinct status (counts toward fusing).
  3. stock_value_em (Eastmoney RPT_VALUEANALYSIS_DET) added as a per-code
     historical-series source: real daily history of PE(TTM)/PB/PS(TTM)/
     market_cap/total_shares sampled at early/mid/recent time points.
  4. Per-code sources fetch ONCE per code (not once per field); batch sources
     fetch the snapshot ONCE. Request accounting is by real network call.
  5. Snapshot responses never carry is_historical_series=True; plain PS never
     mapped to ps_ttm.
  6. Per-request response SHA256 + retrieved_at provenance is mandatory.
  7. NaN / Inf / non-finite / empty / non-numeric values never count as
     PROVEN.
  8. Repeated responses/codes do not double-count requests or coverage.
  9. Network remains blocked by default; unit tests are fully offline.

Exit code: 0 on success, 1 on any failure.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import inspect
import json
import logging
import math
import os
import re
import sqlite3
import sys
import time
from collections import Counter
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from itertools import count as _count
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("probe_bj")

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

NETWORK_ENV_VAR = "NETWORK_PROBE_ENABLED"

TARGET_FIELDS = [
    "pe_ttm",
    "pb_mrq",
    "ps_ttm",
    "market_cap",
    "total_shares",
    "dividend_yield",
]

# Probe result statuses (P2-07B taxonomy).
STATUS_PROVEN = "PROVEN"
STATUS_LIMITED_SAMPLE = "LIMITED_SAMPLE"
STATUS_ADAPTER_ERROR = "ADAPTER_ERROR"
STATUS_NETWORK_ERROR = "NETWORK_ERROR"
STATUS_FIELD_UNAVAILABLE = "FIELD_UNAVAILABLE"
STATUS_CODE_NOT_FOUND = "CODE_NOT_FOUND"
STATUS_RATE_LIMITED = "RATE_LIMITED"
STATUS_UNPROVEN = "UNPROVEN"

# Error-like statuses that count toward source fusing.
FUSABLE_STATUSES = {
    STATUS_ADAPTER_ERROR,
    STATUS_NETWORK_ERROR,
    STATUS_RATE_LIMITED,
}

# Mapping statuses (P2-07B §4.1).
MAPPING_OFFICIAL_PROVEN = "OFFICIAL_PROVEN"
MAPPING_CANDIDATE_NAME_MATCH = "CANDIDATE_NAME_MATCH"
MAPPING_UNPAIRED_OLD = "UNPAIRED_OLD"
MAPPING_UNPAIRED_NEW = "UNPAIRED_NEW"
MAPPING_CONFLICT = "CONFLICT"
MAPPING_UNRESOLVED = "UNRESOLVED"

PER_SOURCE_REQUEST_CAP = 30
TOTAL_REQUEST_CAP = 80
MAX_CODES_DEFAULT = 12
MAX_TIME_MINUTES_DEFAULT = 20

# Dividend yield: no probed source exposes a clear historical series in this
# work order's scope. Recorded as FIELD_UNAVAILABLE (§5.3).
DIVIDEND_YIELD_STATUS = STATUS_FIELD_UNAVAILABLE

# Rate-limit markers (network / provider side). Conservative; matches Chinese
# data APIs and generic rate-limit wording.
RATE_LIMIT_MARKERS = [
    "rate limit", "rate-limit", "too many requests", "429",
    "访问过于频繁", "访问太频繁", "请求过于频繁", "请求太频繁",
    "操作频繁", "frequent", "frequency limit", "限频", "被限制",
    "please slow down", "slow down", "exceeded", "exceed the",
]

# Keys that baidu/comparison adapters look up to signal that the probed code
# is absent from the source (delisted / old 83xxxx code). A KeyError on one of
# these keys is a verified code-absence signal. A KeyError on ANY other key
# means the response structure is unknown / schema drift and is ADAPTER_ERROR.
KNOWN_CODE_ABSENCE_KEYS = frozenset({"chartInfo", "result"})


def _make_source(name, label, mode, fields, timeout_supported, **extra) -> dict:
    """Build a SOURCE_DEFINITIONS entry. `fields` maps target field -> dict of
    source-specific lookup keys (col / indicator / unit / note)."""
    d = {
        "name": name,
        "label": label,
        "mode": mode,
        "fields": fields,
        "timeout_supported": bool(timeout_supported),
        "requires_credentials": False,
        "access_tier": "free",
    }
    d.update(extra)
    return d


def _field(unit: str, col: str | None = None,
           indicator: str | None = None, note: str = "") -> dict:
    return {"unit": unit, "col": col, "indicator": indicator, "note": note}


SOURCE_DEFINITIONS: list[dict[str, Any]] = [
    _make_source(
        "stock_value_em",
        "东方财富-估值分析每日明细(历史序列)",
        "per_code_history",
        {
            "pe_ttm": _field("倍", col="PE(TTM)"),
            "pb_mrq": _field("倍", col="市净率"),
            "ps_ttm": _field(
                "倍", col="市销率",
                note="akshare renames PS_TTM->市销率; underlying Eastmoney field is PS_TTM",
            ),
            "market_cap": _field("元", col="总市值"),
            "total_shares": _field("股", col="总股本"),
        },
        timeout_supported=False,
        endpoint="datacenter-web.eastmoney.com/api/data/v1/get RPT_VALUEANALYSIS_DET",
    ),
    _make_source(
        "stock_bj_a_spot_em",
        "东方财富-京A实时行情",
        "batch",
        {
            "pe_ttm": _field("倍", col="市盈率-动态"),
            "pb_mrq": _field("倍", col="市净率"),
            "market_cap": _field("元", col="总市值"),
            "total_shares": _field("股", col="总股本"),
        },
        timeout_supported=False,
        ps_note="ps_ttm not available from this source",
        code_col="代码",
    ),
    _make_source(
        "stock_zh_valuation_baidu",
        "百度股市通-估值数据",
        "per_code_per_indicator",
        {
            "pe_ttm": _field("倍", indicator="市盈率(TTM)"),
            "pb_mrq": _field("倍", indicator="市净率"),
        },
        timeout_supported=False,
        ps_note="baidu exposes {总市值, 市盈率(TTM), 市盈率(静), 市净率, 市现率}; no PS indicator",
    ),
    _make_source(
        "stock_zh_valuation_comparison_em",
        "东方财富-同行比较估值",
        "per_code_snapshot",
        {
            "pe_ttm": _field("倍", col="市盈率-TTM"),
            "pb_mrq": _field("倍", col="市净率-MRQ"),
            "ps_ttm": _field("倍", col="市销率-TTM"),
        },
        timeout_supported=False,
        ps_note="Source exposes PE-TTM / PB-MRQ / PS-TTM; no total-market-cap column",
        code_col="代码",
    ),
]

BATCH_SOURCES = [s["name"] for s in SOURCE_DEFINITIONS if s["mode"] == "batch"]

# ---------------------------------------------------------------------------
# data classes
# ---------------------------------------------------------------------------


@dataclass
class CodeMapping:
    old_code: str
    new_code: str
    name_old: str = ""
    name_new: str = ""
    mapping_status: str = MAPPING_CANDIDATE_NAME_MATCH
    confidence: str = "medium"
    evidence_type: str = ""
    evidence_url: str = ""
    evidence_date: str = ""
    evidence_sha256: str = ""
    evidence_excerpt: str = ""
    conflicts: list[str] = field(default_factory=list)
    usable_for_probe: bool = True
    usable_for_migration: bool = False
    reason: str = ""

    # Back-compat aliases (legacy B4-R1 fields).
    name: str = ""
    exchange: str = "BJ"
    effective_date: str = ""
    evidence_source: str = "candidate_name_match"
    one_to_one: bool = True


@dataclass
class ProbeRequest:
    code: str
    code_type: str  # old / new
    name: str
    source: str = ""
    field: str = ""
    reason: str = ""


@dataclass
class ProbeResult:
    request_id: str
    code_requested: str
    code_resolved: str
    code_type: str
    name: str
    source: str
    field: str
    value: Any = None
    unit: str = ""
    as_of_date: str = ""
    period_start: str = ""
    period_end: str = ""
    is_historical_series: bool = False
    source_column: str = ""
    source_title_or_endpoint: str = ""
    response_sha256: str = ""
    retrieved_at: str = ""
    status: str = STATUS_UNPROVEN
    error: str = ""
    error_class: str = ""
    latency_ms: float = 0.0
    observation_id: str = ""

    # Back-compat alias.
    @property
    def code(self) -> str:
        return self.code_requested

    @property
    def duration_ms(self) -> float:
        return self.latency_ms


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def open_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True, timeout=30)
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA temp_store=MEMORY")
    if conn.execute("PRAGMA query_only").fetchone()[0] != 1:
        raise RuntimeError(f"read-only verification failed for {path}")
    conn.row_factory = sqlite3.Row
    return conn


def safe_str(val: Any) -> str:
    if val is None:
        return ""
    return str(val)


def now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8", errors="replace"))


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(dict.fromkeys(key for row in rows for key in row.keys()))
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def _df_to_sha(df: Any) -> str:
    """Canonical SHA256 of a DataFrame response (records-orient, ISO dates)."""
    if df is None:
        return sha256_text("null")
    try:
        if hasattr(df, "empty") and df.empty:
            return sha256_text("empty")
        if hasattr(df, "to_json"):
            return sha256_text(str(df.to_json(orient="records", date_format="iso")))
        return sha256_text(str(df))
    except Exception:
        return sha256_text(str(df))


def _is_network_error(exc: BaseException) -> bool:
    """Heuristic: classify an exception as network-level."""
    name = type(exc).__name__
    msg = str(exc)
    network_markers = [
        "ConnectionError", "TimeoutError", "RemoteDisconnected",
        "Connection aborted", "Remote end closed", "timed out", "timeout",
        "URLError", "SSLError", "socket", "ConnectionResetError",
        "Max retries", "Failed to establish", "getaddrinfo", "BadStatusLine",
        "IncompleteRead",
    ]
    if name in ("ConnectionError", "TimeoutError", "URLError", "SSLError",
                "ConnectionResetError", "RemoteDisconnected", "IncompleteRead"):
        return True
    low = msg.lower()
    return any(marker.lower() in low for marker in network_markers)


def _is_rate_limited(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(marker.lower() in msg for marker in RATE_LIMIT_MARKERS)


def _classify_call_error(exc: BaseException) -> tuple[str, str]:
    """Map an exception to (status, error_message)."""
    if _is_rate_limited(exc):
        return STATUS_RATE_LIMITED, f"{type(exc).__name__}: {exc}"
    if _is_network_error(exc):
        return STATUS_NETWORK_ERROR, f"{type(exc).__name__}: {exc}"
    return STATUS_ADAPTER_ERROR, f"{type(exc).__name__}: {exc}"


def _classify_probe_error(err_str: str) -> tuple[str, str]:
    """Map an akshare error string to (status, error_message).

    P2-07B classification:
      - rate-limit markers => RATE_LIMITED (counts toward fusing).
      - KeyError on a KNOWN code-absence key (chartInfo/result) => the probed
        code is not present in the source => CODE_NOT_FOUND (does NOT fuse).
      - KeyError on any other / unknown key => response schema drift =>
        ADAPTER_ERROR (counts toward source fusing).
      - network-ish errors => NETWORK_ERROR.
      - everything else => ADAPTER_ERROR.
    """
    low = err_str.lower()
    if any(marker in low for marker in RATE_LIMIT_MARKERS):
        return STATUS_RATE_LIMITED, err_str
    m = re.match(r"KeyError:\s*(.+)$", err_str)
    if m:
        key = m.group(1).strip().strip("'\"")
        if key in KNOWN_CODE_ABSENCE_KEYS:
            return STATUS_CODE_NOT_FOUND, err_str
        return STATUS_ADAPTER_ERROR, (
            f"KeyError on unknown response key {key!r} (schema drift / "
            f"unexpected structure): {err_str}"
        )
    status, msg = _classify_call_error(RuntimeError(err_str))
    return status, msg


def _error_class_from_str(err_str: str) -> str:
    if not err_str:
        return ""
    return err_str.split(":")[0].strip() or "Exception"


def _func_accepts_timeout(func: Any) -> bool:
    """True if the callable accepts a `timeout` keyword argument."""
    try:
        sig = inspect.signature(func)
    except (ValueError, TypeError):
        return False
    params = sig.parameters
    if "timeout" in params:
        return True
    return any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())


def _source_accepts_timeout(src: dict) -> bool:
    """Timeout policy from explicit adapter table (SOURCE_DEFINITIONS)."""
    return bool(src.get("timeout_supported"))


def _safe_network_call(ak, func_name: str, src_def: dict | None = None, *args, **kwargs) -> Any:
    """Call an akshare function WITHOUT unconditional timeout injection.

    P2-07B: `timeout` is only added when the target function supports it
    (checked via inspect.signature() and the explicit adapter table).
    """
    func = getattr(ak, func_name, None)
    if func is None:
        return None, f"akshare has no {func_name}"
    if src_def is not None:
        if _source_accepts_timeout(src_def) and _func_accepts_timeout(func):
            kwargs.setdefault("timeout", 30)
    elif _func_accepts_timeout(func):
        kwargs.setdefault("timeout", 30)
    try:
        result = func(*args, **kwargs)
        return result, None
    except Exception as e:
        err_str = f"{type(e).__name__}: {e}"
        return None, err_str


def _network_call_with_retry(ak, func_name: str, src_def: dict, *args, **kwargs):
    """Call akshare with at most ONE retry, only on network/timeout errors.

    Returns (result, err_str, calls_made). Every real network attempt is
    counted in calls_made (P2-07B §5.2 request accounting).
    """
    result, err = _safe_network_call(ak, func_name, src_def, *args, **kwargs)
    calls = 1
    if err is not None:
        status, _ = _classify_probe_error(err)
        if status == STATUS_NETWORK_ERROR:
            result2, err2 = _safe_network_call(ak, func_name, src_def, *args, **kwargs)
            calls += 1
            if err2 is None:
                return result2, None, calls
            result, err = result2, err2
    return result, err, calls


def _record_request_attempts(
    ledger: list[dict] | None,
    req_counter: Any,
    source: str,
    endpoint: str,
    code_scope: str,
    calls: int,
    started_at: str,
    finished_at: str,
    latency_ms: float,
    response_sha256: str,
    final_error: str,
) -> str:
    """Record one ledger row per real network attempt and return the final id.

    The retry wrapper only retries network failures. If two calls were made,
    the first row is therefore a failed network attempt and the second row
    carries the final response/error. Observations reference the final attempt.
    """
    request_ids: list[str] = []
    for attempt_no in range(1, calls + 1):
        request_id = f"{source}-{next(req_counter):04d}"
        request_ids.append(request_id)
        is_final = attempt_no == calls
        error = final_error if is_final else "network failure; retried once"
        outcome = (
            _classify_probe_error(error)[0] if error
            else STATUS_PROVEN
        )
        row = {
            "request_id": request_id,
            "source": source,
            "endpoint": endpoint,
            "request_started_at": started_at,
            "request_finished_at": finished_at,
            "attempt_no": attempt_no,
            "is_retry": attempt_no > 1,
            "code_or_batch_scope": code_scope,
            "outcome": outcome,
            "latency_ms": round(latency_ms / max(calls, 1), 3),
            "response_sha256": response_sha256 if is_final else "",
            "error_class": _error_class_from_str(error),
            "error": error,
            "counted_as_real_request": True,
        }
        if ledger is not None:
            ledger.append(row)
    return request_ids[-1] if request_ids else ""


# ---------------------------------------------------------------------------
# Phase 1 — Local mapping (P2-07B §4 schema)
# ---------------------------------------------------------------------------


def _classify_mapping(evidence_source: str, conflicts: list[str]) -> str:
    if conflicts:
        return MAPPING_CONFLICT
    if evidence_source == "unpaired_new_code":
        return MAPPING_UNPAIRED_NEW
    if evidence_source == "unpaired_old_code":
        return MAPPING_UNPAIRED_OLD
    return MAPPING_CANDIDATE_NAME_MATCH


def build_code_mapping(market_db: Path) -> list[CodeMapping]:
    conn = open_ro(market_db)
    try:
        old_rows = conn.execute(
            'SELECT code, name, list_date FROM stock_info WHERE market = ? ORDER BY code',
            ("BJ",),
        ).fetchall()
        new_rows = conn.execute(
            'SELECT code, name, list_date FROM stock_info WHERE market = ? ORDER BY code',
            ("北交所",),
        ).fetchall()
    finally:
        conn.close()

    old_by_name: dict[str, dict] = {r["name"]: dict(r) for r in old_rows}
    new_by_name: dict[str, dict] = {r["name"]: dict(r) for r in new_rows}

    mappings: list[CodeMapping] = []
    matched_names = set()

    for name, old_rec in old_by_name.items():
        if name in new_by_name:
            new_rec = new_by_name[name]
            conflicts: list[str] = []
            one_to_one = True

            other_old = [r["code"] for r in old_rows if r["name"] == name and r["code"] != old_rec["code"]]
            if other_old:
                conflicts.append(f"multiple old codes: {other_old}")
                one_to_one = False

            other_new = [r["code"] for r in new_rows if r["name"] == name and r["code"] != new_rec["code"]]
            if other_new:
                conflicts.append(f"multiple new codes: {other_new}")
                one_to_one = False

            effective = old_rec["list_date"] or new_rec["list_date"] or ""
            # P2-07B: name match alone is a CANDIDATE, not authoritative
            # evidence. confidence is medium (not high).
            confidence = "medium"

            mappings.append(CodeMapping(
                old_code=old_rec["code"],
                new_code=new_rec["code"],
                name_old=name,
                name_new=name,
                mapping_status=MAPPING_CANDIDATE_NAME_MATCH,
                confidence=confidence,
                evidence_type="none_local",
                evidence_url="",
                evidence_date="",
                evidence_sha256="",
                evidence_excerpt="",
                conflicts=conflicts,
                usable_for_probe=True,
                usable_for_migration=False,
                reason=(
                    "same stock name in BJ(old) and 北交所(new) stock_info; "
                    "name match only, no official exchange/regulator/company "
                    "disclosure evidence; probe-only, not for migration"
                ),
                name=name,
                exchange="BJ",
                effective_date=effective,
                evidence_source="candidate_name_match",
                one_to_one=one_to_one,
            ))
            matched_names.add(name)

    for name, new_rec in new_by_name.items():
        if name not in matched_names:
            mappings.append(CodeMapping(
                old_code="",
                new_code=new_rec["code"],
                name_old="",
                name_new=name,
                mapping_status=MAPPING_UNPAIRED_NEW,
                confidence="low",
                evidence_type="none_local",
                usable_for_probe=True,
                usable_for_migration=False,
                reason="new code exists without a paired old code in stock_info",
                name=name,
                exchange="北交所",
                effective_date=new_rec["list_date"] or "",
                evidence_source="unpaired_new_code",
                one_to_one=True,
            ))

    for name, old_rec in old_by_name.items():
        if name not in matched_names:
            mappings.append(CodeMapping(
                old_code=old_rec["code"],
                new_code="",
                name_old=name,
                name_new="",
                mapping_status=MAPPING_UNPAIRED_OLD,
                confidence="low",
                evidence_type="none_local",
                conflicts=["may be delisted or transferred"],
                usable_for_probe=True,
                usable_for_migration=False,
                reason="old code exists without a paired new code in stock_info",
                name=name,
                exchange="BJ",
                effective_date=old_rec["list_date"] or "",
                evidence_source="unpaired_old_code",
                one_to_one=True,
            ))

    # 920808 (曙光数创): P2-02A audit notes Tencent reports it as market=BJ
    # while the formal DB labels it market=SZ (single secondary source).
    # Explicitly UNRESOLVED: probe-only, never auto-migrate.
    has_920808 = any(m.new_code == "920808" for m in mappings)
    if not has_920808:
        mappings.append(CodeMapping(
            old_code="",
            new_code="920808",
            name_old="",
            name_new="曙光数创",
            mapping_status=MAPPING_UNRESOLVED,
            confidence="low",
            evidence_type="secondary_market_label_conflict",
            evidence_url="",
            evidence_date="",
            evidence_sha256="",
            evidence_excerpt=(
                "P2-02A audit: Tencent reports 920808 as market=BJ while formal "
                "qs.db stock_info labels market=SZ; single secondary source"
            ),
            conflicts=["formal DB market=SZ, secondary source reports BJ"],
            usable_for_probe=True,
            usable_for_migration=False,
            reason="market-label discrepancy between formal DB and a single "
                   "secondary source; unresolved, probe-only",
            name="曙光数创",
            exchange="SZ",
            effective_date="",
            evidence_source="unresolved",
            one_to_one=True,
        ))

    mappings.sort(key=lambda m: m.new_code or m.old_code)
    return mappings


def apply_official_mapping_evidence(
    mappings: list[CodeMapping], evidence_rows: list[dict],
) -> list[CodeMapping]:
    """Upgrade only mappings backed by a complete official evidence record."""
    by_pair = {(m.old_code, m.new_code): m for m in mappings}
    for evidence in evidence_rows:
        required = (
            "old_code", "new_code", "security_name", "listing_date",
            "evidence_url", "evidence_retrieved_at", "evidence_sha256",
            "evidence_excerpt",
        )
        if not all(evidence.get(k) for k in required):
            continue
        sha = str(evidence["evidence_sha256"])
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            continue
        pair = (str(evidence["old_code"]), str(evidence["new_code"]))
        name = str(evidence["security_name"])
        mapping = by_pair.get(pair)
        if mapping is None:
            same_new = [m for m in mappings if m.new_code == pair[1]]
            same_old = [m for m in mappings if m.old_code == pair[0]]
            compatible = [
                m for m in same_new + same_old
                if (not m.name or m.name == name)
                and (not m.name_old or m.name_old == name)
                and (not m.name_new or m.name_new == name)
            ]
            compatible = list({id(m): m for m in compatible}.values())
            collisions = list({id(m): m for m in same_new + same_old}.values())
            incompatible = [m for m in collisions if m not in compatible]
            if compatible and not incompatible:
                mapping = next(
                    (m for m in compatible if m.old_code == pair[0]), compatible[0],
                )
                for duplicate in compatible:
                    if duplicate is not mapping:
                        mappings.remove(duplicate)
                mapping.old_code = pair[0]
                mapping.new_code = pair[1]
            elif not collisions:
                mapping = CodeMapping(old_code=pair[0], new_code=pair[1])
                mappings.append(mapping)
            else:
                continue
            by_pair[pair] = mapping
        mapping.name_old = name
        mapping.name_new = name
        mapping.name = name
        mapping.mapping_status = MAPPING_OFFICIAL_PROVEN
        mapping.confidence = "high"
        mapping.evidence_type = "bse_official_mapping_attachment"
        mapping.evidence_url = str(evidence["evidence_url"])
        mapping.evidence_date = str(evidence["evidence_retrieved_at"])
        mapping.evidence_sha256 = sha
        mapping.evidence_excerpt = str(evidence["evidence_excerpt"])
        mapping.conflicts = []
        mapping.usable_for_probe = True
        mapping.usable_for_migration = True
        mapping.reason = "official Beijing Stock Exchange old/new code mapping"
        mapping.evidence_source = "Beijing Stock Exchange"
        mapping.effective_date = str(evidence["listing_date"])
        mapping.one_to_one = True
    mappings.sort(key=lambda m: m.new_code or m.old_code)
    return mappings


def phase1(
    market_db: Path, output_dir: Path,
    official_evidence: list[dict] | None = None,
) -> list[CodeMapping]:
    log.info("Phase 1: building BJ code mapping from %s", market_db)
    mappings = build_code_mapping(market_db)
    if official_evidence:
        mappings = apply_official_mapping_evidence(mappings, official_evidence)
    log.info("  -> %d mapping entries", len(mappings))

    json_path = output_dir / "bj-code-mapping.json"
    csv_path = output_dir / "bj-code-mapping.csv"

    dicts = [asdict(m) for m in mappings]
    write_json(json_path, dicts)
    write_csv(csv_path, dicts)

    log.info("  wrote %s", json_path)
    log.info("  wrote %s", csv_path)
    return mappings


# ---------------------------------------------------------------------------
# Phase 2 — Network probe
# ---------------------------------------------------------------------------


def _probe_import_akshare():
    try:
        import akshare as ak
        return ak
    except ImportError:
        log.error("akshare not installed")
        return None


# Curated manual-review names (P2-07B §4.3) — 3 medium candidates.
MANUAL_REVIEW_MEDIUM_NAMES = ["艾融软件", "贝特瑞", "星昊医药"]
# Unpaired/conflict old codes required in the review set.
MANUAL_REVIEW_UNPAIRED_CODES = ["832317", "833874", "833994"]
# 920808 always probed (UNRESOLVED).
MANUAL_REVIEW_SPECIAL_CODES = ["920808"]


def _build_probe_plan(mappings: list[CodeMapping], max_codes: int) -> list[ProbeRequest]:
    """Build the probe plan. Prioritizes the P2-07B manual-review set:
    medium candidates (old+new), 920808, unpaired/conflict codes, then fills
    with new-only and old-only codes up to max_codes."""
    if max_codes <= 0:
        return []

    by_code: dict[str, CodeMapping] = {}
    for m in mappings:
        if m.old_code:
            by_code.setdefault(m.old_code, m)
        if m.new_code:
            by_code.setdefault(m.new_code, m)

    plan: list[ProbeRequest] = []
    codes_seen: set[str] = set()

    def add_code(code: str, reason: str) -> None:
        if not code or code in codes_seen:
            return
        m = by_code.get(code)
        if m is None:
            return
        code_type = "new" if m.new_code == code else "old"
        codes_seen.add(code)
        plan.append(ProbeRequest(
            code=code, code_type=code_type, name=m.name or m.name_old or m.name_new,
            reason=reason,
        ))

    # 1. Curated old/new pairs remain in the probe plan after official evidence
    # upgrades them from CANDIDATE_NAME_MATCH to OFFICIAL_PROVEN.
    name_to_mapping = {
        m.name: m for m in mappings
        if m.mapping_status in (MAPPING_CANDIDATE_NAME_MATCH, MAPPING_OFFICIAL_PROVEN)
    }
    for nm in MANUAL_REVIEW_MEDIUM_NAMES:
        m = name_to_mapping.get(nm)
        if m and m.old_code and m.new_code:
            add_code(m.old_code, "medium_candidate")
            add_code(m.new_code, "medium_candidate")

    # 2. 920808 special (UNRESOLVED).
    for code in MANUAL_REVIEW_SPECIAL_CODES:
        add_code(code, "unresolved_special")

    # 3. unpaired/conflict codes.
    for code in MANUAL_REVIEW_UNPAIRED_CODES:
        add_code(code, "unpaired_or_conflict")

    # 4. new-only codes (fill), then old-only codes (fill).
    new_only = [m for m in mappings if m.old_code == "" and m.new_code]
    old_only = [m for m in mappings if m.new_code == "" and m.old_code]
    for pool, reason in ((new_only, "new_only"), (old_only, "old_only")):
        for m in pool:
            if len(plan) >= max_codes:
                break
            add_code(m.new_code or m.old_code, reason)

    # 5. any remaining one-to-one to fill.
    if len(plan) < max_codes:
        for m in mappings:
            if len(plan) >= max_codes:
                break
            add_code(m.old_code, "one_to_one_fill")
            if len(plan) < max_codes:
                add_code(m.new_code, "one_to_one_fill")

    return plan[:max_codes]


def _fuse_source(name: str, label: str, reason: str) -> dict:
    log.warning("FUSE source %s: %s", name, reason)
    result = {
        "source": name,
        "label": label,
        "fused": True,
        "fuse_reason": reason,
        "fields": {f: STATUS_UNPROVEN for f in TARGET_FIELDS},
        "requests_made": 0,
        "consecutive_failures": 0,
    }
    return result


def _classify_field(probe_results: list[ProbeResult], field: str) -> str:
    related = [r for r in probe_results if r.field == field and r.source]
    if not related:
        return STATUS_UNPROVEN
    proven = [r for r in related if r.status == STATUS_PROVEN]
    limited = [r for r in related if r.status == STATUS_LIMITED_SAMPLE]
    field_unavailable = [r for r in related if r.status == STATUS_FIELD_UNAVAILABLE]
    code_not_found = [r for r in related if r.status == STATUS_CODE_NOT_FOUND]
    errors = [r for r in related if r.status in FUSABLE_STATUSES]

    if len(proven) >= 3:
        return STATUS_PROVEN
    if proven:
        return STATUS_LIMITED_SAMPLE
    if limited:
        return STATUS_LIMITED_SAMPLE
    if field_unavailable:
        return STATUS_FIELD_UNAVAILABLE
    if code_not_found:
        return STATUS_CODE_NOT_FOUND
    if errors:
        if any(r.status == STATUS_NETWORK_ERROR for r in errors):
            return STATUS_NETWORK_ERROR
        if any(r.status == STATUS_RATE_LIMITED for r in errors):
            return STATUS_RATE_LIMITED
        return STATUS_ADAPTER_ERROR
    return STATUS_UNPROVEN


def _finite_float(val: Any) -> float | None:
    """Return float if finite (not NaN / Inf / None / empty / non-numeric)."""
    if val is None:
        return None
    s = str(val).strip()
    if s in ("", "-", "nan", "NaN", "None"):
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return f


def _probe_batch_source(
    ak_module: Any,
    src_def: dict,
    requests: list[ProbeRequest],
    req_counter: Any,
    ledger: list[dict] | None = None,
) -> tuple[list[ProbeResult], str | None, int]:
    """Fetch a batch snapshot ONCE and probe all codes/fields from it.

    Counts as a single network request (plus at most one retry). Returns
    (results, error_or_None, calls_made).
    """
    src_name = src_def["name"]
    started_at = now_iso()
    start = time.monotonic()
    df, err, calls = _network_call_with_retry(ak_module, src_name, src_def)
    latency = (time.monotonic() - start) * 1000
    endpoint = f"akshare.{src_name}"
    sha = _df_to_sha(df)
    retrieved = now_iso()
    request_id = _record_request_attempts(
        ledger, req_counter, src_name, endpoint, "batch", calls,
        started_at, retrieved, latency, sha, err or "",
    )
    obs_counter = _count(1)

    def mk(req: ProbeRequest, field: str, **overrides) -> ProbeResult:
        base = dict(
            request_id=request_id,
            code_requested=req.code,
            code_resolved=req.code,
            code_type=req.code_type,
            name=req.name,
            source=src_name,
            field=field,
            value=None,
            unit="",
            as_of_date="",
            period_start="",
            period_end="",
            is_historical_series=False,
            source_column="",
            source_title_or_endpoint=endpoint,
            response_sha256=sha,
            retrieved_at=retrieved,
            status=STATUS_UNPROVEN,
            error="",
            error_class="",
            latency_ms=latency,
            observation_id=f"obs-{src_name}-{next(obs_counter):05d}",
        )
        base.update(overrides)
        return ProbeResult(**base)

    if err:
        status, _ = _classify_probe_error(err)
        results = [
            mk(req, "", status=status, error=err,
               error_class=_error_class_from_str(err))
            for req in requests
        ]
        return results, err, calls

    if df is None or df.empty:
        results = [
            mk(req, "", status=STATUS_FIELD_UNAVAILABLE, error="empty response")
            for req in requests
        ]
        return results, None, calls

    code_col = src_def.get("code_col", "代码")
    field_cols = {f: d.get("col") for f, d in src_def["fields"].items()}
    results = []

    if code_col not in df.columns:
        for req in requests:
            for field, _ in field_cols.items():
                r = mk(req, field)
                r.status = STATUS_FIELD_UNAVAILABLE
                r.error = f"code column '{code_col}' not in response"
                r.error_class = "schema_drift"
                results.append(r)
        return results, None, calls

    present_codes = set(df[code_col].astype(str))
    for req in requests:
        if req.code not in present_codes:
            r = mk(req, "")
            r.status = STATUS_CODE_NOT_FOUND
            r.error = f"code {req.code} not in {src_name} response"
            results.append(r)
            continue
        row = df[df[code_col].astype(str) == req.code].iloc[0]
        for field, col in field_cols.items():
            r = mk(req, field)
            if not col or col not in df.columns:
                r.status = STATUS_FIELD_UNAVAILABLE
                r.error = f"column '{col}' not in response"
                r.error_class = "schema_drift"
                results.append(r)
                continue
            r.source_column = col
            fval = _finite_float(row.get(col))
            if fval is None:
                r.status = STATUS_FIELD_UNAVAILABLE
                r.error = f"field {col} empty/non-finite for {req.code}"
                results.append(r)
            else:
                r.value = fval
                r.status = STATUS_PROVEN
                results.append(r)
    return results, None, calls


def _probe_value_em(
    ak_module: Any,
    req: ProbeRequest,
    src_def: dict,
    req_counter: Any,
    ledger: list[dict] | None = None,
) -> tuple[list[ProbeResult], int]:
    """Probe stock_value_em (Eastmoney RPT_VALUEANALYSIS_DET): real daily
    historical series per code. One network request per code.

    Samples at least 3 time points (early / middle / most recent available
    date). Returns (results, calls_made).
    """
    src_name = src_def["name"]
    started_at = now_iso()
    start = time.monotonic()
    df, err, calls = _network_call_with_retry(
        ak_module, "stock_value_em", src_def, symbol=req.code,
    )
    latency = (time.monotonic() - start) * 1000
    endpoint = "akshare.stock_value_em (datacenter-web.eastmoney.com RPT_VALUEANALYSIS_DET)"
    sha = _df_to_sha(df)
    retrieved = now_iso()
    request_id = _record_request_attempts(
        ledger, req_counter, src_name, endpoint, req.code, calls,
        started_at, retrieved, latency, sha, err or "",
    )
    obs_counter = _count(1)

    def mk(field: str) -> ProbeResult:
        return ProbeResult(
            request_id=request_id,
            code_requested=req.code, code_resolved=req.code,
            code_type=req.code_type, name=req.name,
            source=src_name, field=field, value=None, unit="",
            as_of_date="", period_start="", period_end="",
            is_historical_series=False, source_column="",
            source_title_or_endpoint=endpoint, response_sha256=sha,
            retrieved_at=retrieved, status=STATUS_UNPROVEN,
            latency_ms=latency,
            observation_id=f"obs-{src_name}-{req.code}-{next(obs_counter):05d}",
        )

    if err:
        status, _ = _classify_probe_error(err)
        # Old 83xxxx/43xxxx codes raise TypeError ('NoneType' object is not
        # subscriptable) inside akshare because Eastmoney returns result=null.
        # That is a response-structure signal, not a verified absence key, so
        # it is ADAPTER_ERROR (not CODE_NOT_FOUND) per §5.4.
        results = [mk("")]
        results[0].status = status
        results[0].error = err
        results[0].error_class = _error_class_from_str(err)
        return results, calls

    if df is None or df.empty:
        results = [mk("")]
        results[0].status = STATUS_FIELD_UNAVAILABLE
        results[0].error = "empty response"
        return results, calls

    date_col = "数据日期"
    if date_col not in df.columns:
        results = [mk("")]
        results[0].status = STATUS_ADAPTER_ERROR
        results[0].error = f"date column '{date_col}' not in stock_value_em response"
        results[0].error_class = "schema_drift"
        return results, calls

    # Parse dates, drop invalid, sort ascending.
    dates = []
    for v in df[date_col]:
        ds = str(v)[:10]
        dates.append(ds)
    rows = list(zip(dates, df.to_dict(orient="records")))
    rows = [r for r in rows if len(r[0]) == 10]
    if len(rows) < 3:
        results = [mk("")]
        results[0].status = STATUS_LIMITED_SAMPLE
        results[0].error = f"only {len(rows)} dated rows; cannot sample 3 time points"
        return results, calls

    rows.sort(key=lambda r: r[0])
    period_start = rows[0][0]
    period_end = rows[-1][0]

    # Sample early / middle / recent.
    idxs = sorted({0, len(rows) // 2, len(rows) - 1})
    field_cols = {f: d.get("col") for f, d in src_def["fields"].items()}

    results: list[ProbeResult] = []
    emitted: set[tuple] = set()
    for idx in idxs:
        as_of, record = rows[idx]
        for field, col in field_cols.items():
            if not col or col not in record:
                continue
            r = mk(field)
            r.as_of_date = as_of
            r.period_start = period_start
            r.period_end = period_end
            r.is_historical_series = True
            r.source_column = col
            fval = _finite_float(record.get(col))
            if fval is None:
                r.status = STATUS_FIELD_UNAVAILABLE
                r.error = f"field {col} empty/non-finite on {as_of}"
                results.append(r)
                continue
            r.value = fval
            r.unit = src_def["fields"][field]["unit"]
            r.status = STATUS_PROVEN
            key = (src_name, req.code, field, as_of)
            if key in emitted:
                continue
            emitted.add(key)
            results.append(r)

    # dividend_yield: no historical series from this source.
    dy = mk("dividend_yield")
    dy.status = STATUS_FIELD_UNAVAILABLE
    dy.error = "no dividend_yield column in stock_value_em"
    results.append(dy)

    if not any(r.status == STATUS_PROVEN for r in results):
        results[0].status = STATUS_FIELD_UNAVAILABLE
        results[0].error = "no usable fields in stock_value_em response"

    return results, calls


def _probe_valuation_baidu(
    ak_module: Any,
    req: ProbeRequest,
    field: str,
    src_def: dict,
    req_counter: Any,
    ledger: list[dict] | None = None,
) -> tuple[list[ProbeResult], int]:
    """Probe stock_zh_valuation_baidu for one (code, indicator). One request
    per (code, indicator); returns history sampled at 3 time points."""
    src_name = src_def["name"]
    started_at = now_iso()
    start = time.monotonic()
    field_def = src_def["fields"].get(field, {})
    indicator = field_def.get("indicator")
    request_id = ""
    obs_counter = _count(1)

    def mk(field_: str) -> ProbeResult:
        return ProbeResult(
            request_id=request_id,
            code_requested=req.code, code_resolved=req.code,
            code_type=req.code_type, name=req.name,
            source=src_name, field=field_, value=None,
            unit=field_def.get("unit", ""),
            source_title_or_endpoint="akshare.stock_zh_valuation_baidu (百度股市通)",
            latency_ms=0.0,
            observation_id=f"obs-{src_name}-{req.code}-{field_}-{next(obs_counter):05d}",
        )

    if not indicator:
        r = mk(field)
        r.status = STATUS_FIELD_UNAVAILABLE
        r.error = f"baidu does not expose indicator for {field}"
        return [r], 0

    df, err, calls = _network_call_with_retry(
        ak_module, "stock_zh_valuation_baidu", src_def,
        symbol=req.code, indicator=indicator, period="近一年",
    )
    latency = (time.monotonic() - start) * 1000
    sha = _df_to_sha(df)
    retrieved = now_iso()
    endpoint = "akshare.stock_zh_valuation_baidu (百度股市通)"
    request_id = _record_request_attempts(
        ledger, req_counter, src_name, endpoint,
        f"{req.code}:{field}", calls, started_at, retrieved,
        latency, sha, err or "",
    )

    if err:
        status, _ = _classify_probe_error(err)
        r = mk(field)
        r.status = status
        r.error = err
        r.error_class = _error_class_from_str(err)
        r.response_sha256 = sha
        r.retrieved_at = retrieved
        r.latency_ms = latency
        return [r], calls

    if df is None or df.empty:
        r = mk(field)
        r.status = STATUS_FIELD_UNAVAILABLE
        r.error = "empty dataframe"
        r.response_sha256 = sha
        r.retrieved_at = retrieved
        r.latency_ms = latency
        return [r], calls

    val_col = [c for c in df.columns if "值" in c or "value" in c.lower() or c in (indicator,)]
    if not val_col:
        r = mk(field)
        r.status = STATUS_FIELD_UNAVAILABLE
        r.error = f"no value column found in baidu response for {indicator}"
        r.error_class = "schema_drift"
        r.response_sha256 = sha
        r.retrieved_at = retrieved
        r.latency_ms = latency
        return [r], calls

    # date column: try 日期 / date / value date
    date_col = next((c for c in df.columns if "日期" in c or "date" in c.lower()), None)
    series = list(df.iterrows())
    if date_col:
        series.sort(key=lambda t: str(t[1][date_col])[:10])
    idxs = sorted({0, len(series) // 2, len(series) - 1})
    results: list[ProbeResult] = []
    for idx in idxs:
        r = mk(field)
        raw_date = str(series[idx][1][date_col])[:10] if date_col else ""
        r.as_of_date = raw_date
        r.period_start = str(series[0][1][date_col])[:10] if date_col else ""
        r.period_end = str(series[-1][1][date_col])[:10] if date_col else ""
        r.is_historical_series = bool(date_col)
        r.source_column = val_col[0]
        r.response_sha256 = sha
        r.retrieved_at = retrieved
        r.latency_ms = latency
        fval = _finite_float(series[idx][1][val_col[0]])
        if fval is None:
            r.status = STATUS_FIELD_UNAVAILABLE
            r.error = f"value empty/non-finite for {indicator}"
        else:
            r.value = fval
            r.status = STATUS_PROVEN
        results.append(r)
    return results, calls


def _probe_valuation_comparison(
    ak_module: Any,
    req: ProbeRequest,
    src_def: dict,
    req_counter: Any,
    ledger: list[dict] | None = None,
) -> tuple[list[ProbeResult], int]:
    """Probe stock_zh_valuation_comparison_em: ONE request per code returning
    PE-TTM / PB-MRQ / PS-TTM. Snapshot (is_historical_series=False)."""
    src_name = src_def["name"]
    started_at = now_iso()
    start = time.monotonic()
    em_symbol = f"BJ{req.code}"
    df, err, calls = _network_call_with_retry(
        ak_module, "stock_zh_valuation_comparison_em", src_def, symbol=em_symbol,
    )
    latency = (time.monotonic() - start) * 1000
    sha = _df_to_sha(df)
    retrieved = now_iso()
    endpoint = "akshare.stock_zh_valuation_comparison_em (东方财富同行比较)"
    request_id = _record_request_attempts(
        ledger, req_counter, src_name, endpoint, req.code, calls,
        started_at, retrieved, latency, sha, err or "",
    )
    obs_counter = _count(1)

    def mk(field_: str) -> ProbeResult:
        return ProbeResult(
            request_id=request_id,
            code_requested=req.code, code_resolved=req.code,
            code_type=req.code_type, name=req.name,
            source=src_name, field=field_, value=None,
            unit=src_def["fields"].get(field_, {}).get("unit", ""),
            is_historical_series=False,
            source_title_or_endpoint=endpoint, response_sha256=sha,
            retrieved_at=retrieved, latency_ms=latency,
            observation_id=f"obs-{src_name}-{req.code}-{next(obs_counter):05d}",
        )

    if err:
        status, _ = _classify_probe_error(err)
        r = mk("")
        r.status = status
        r.error = err
        r.error_class = _error_class_from_str(err)
        return [r], calls

    if df is None or df.empty:
        r = mk("")
        r.status = STATUS_FIELD_UNAVAILABLE
        r.error = "empty dataframe"
        return [r], calls

    code_col = src_def.get("code_col", "代码")
    results: list[ProbeResult] = []
    if code_col not in df.columns:
        for field in src_def["fields"]:
            r = mk(field)
            r.status = STATUS_FIELD_UNAVAILABLE
            r.error = f"code column '{code_col}' not in response"
            r.error_class = "schema_drift"
            results.append(r)
        return results, calls

    val_row = df[df[code_col].astype(str) == req.code] if code_col in df.columns else df.iloc[0:1]
    if val_row.empty:
        r = mk("")
        r.status = STATUS_CODE_NOT_FOUND
        r.error = f"code {req.code} not in comparison"
        return [r], calls

    row = val_row.iloc[0]
    for field, d in src_def["fields"].items():
        col = d.get("col")
        r = mk(field)
        if not col or col not in df.columns:
            r.status = STATUS_FIELD_UNAVAILABLE
            r.error = f"column {col} not in response"
            r.error_class = "schema_drift"
            results.append(r)
            continue
        r.source_column = col
        fval = _finite_float(row.get(col))
        if fval is None:
            r.status = STATUS_FIELD_UNAVAILABLE
            r.error = f"field {col} empty/non-finite"
            results.append(r)
            continue
        r.value = fval
        r.status = STATUS_PROVEN
        results.append(r)
    return results, calls


def _run_probe_requests(
    requests: list[ProbeRequest],
    ak_module: Any,
    output_dir: Path,
    max_requests: int,
    max_time_minutes: int,
    checkpoint_path: Path | None = None,
    resume: bool = False,
) -> tuple[list[ProbeResult], list[dict]]:
    deadline = time.monotonic() + max_time_minutes * 60
    all_results: list[ProbeResult] = []
    request_ledger: list[dict] = []
    source_stats: dict[str, dict] = {}

    for src in SOURCE_DEFINITIONS:
        source_stats[src["name"]] = {
            "source": src["name"],
            "label": src["label"],
            "mode": src["mode"],
            "fused": False,
            "fuse_reason": "",
            "requests_made": 0,
            "consecutive_failures": 0,
            "last_error_type": "",
        }

    total_requests_made = 0
    consecutive_error_map: dict[str, int] = {}
    last_error_type_map: dict[str, str] = {}
    completed_codes: set[str] = set()
    completed_operations: set[str] = set()
    batch_completed = False
    request_budget = min(max_requests, TOTAL_REQUEST_CAP)

    unique_codes = []
    seen = set()
    for r in requests:
        if r.code not in seen:
            seen.add(r.code)
            unique_codes.append(r.code)
    code_to_req = {r.code: r for r in requests}

    if checkpoint_path and resume:
        if not checkpoint_path.is_file():
            raise FileNotFoundError(f"checkpoint not found: {checkpoint_path}")
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        expected_codes = unique_codes
        if checkpoint.get("plan_codes") != expected_codes:
            raise RuntimeError("checkpoint probe plan does not match current plan")
        all_results = [ProbeResult(**row) for row in checkpoint.get("observations", [])]
        request_ledger = list(checkpoint.get("request_ledger", []))
        completed_codes = set(checkpoint.get("completed_codes", []))
        completed_operations = set(checkpoint.get("completed_operations", []))
        batch_completed = bool(checkpoint.get("batch_completed"))
        for row in checkpoint.get("source_stats", []):
            if row.get("source") in source_stats:
                source_stats[row["source"]] = row
        total_requests_made = sum(
            1 for row in request_ledger if row.get("counted_as_real_request")
        )
        consecutive_error_map = {
            name: int(stat.get("consecutive_failures", 0))
            for name, stat in source_stats.items()
        }
        last_error_type_map = {
            name: str(stat.get("last_error_type", ""))
            for name, stat in source_stats.items()
        }
        log.info(
            "  resumed checkpoint: completed_codes=%d, requests=%d",
            len(completed_codes), total_requests_made,
        )

    existing_numbers = []
    for row in request_ledger:
        match = re.search(r"-(\d+)$", str(row.get("request_id", "")))
        if match:
            existing_numbers.append(int(match.group(1)))
    req_counter = _count(max(existing_numbers, default=0) + 1)

    def save_checkpoint() -> None:
        if checkpoint_path is None:
            return
        write_json(checkpoint_path, {
            "version": 1,
            "updated_at": now_iso(),
            "plan_codes": unique_codes,
            "batch_completed": batch_completed,
            "completed_codes": sorted(completed_codes),
            "completed_operations": sorted(completed_operations),
            "request_ledger": request_ledger,
            "observations": [asdict(r) for r in all_results],
            "source_stats": list(source_stats.values()),
        })

    def budget_ok(src_name: str) -> bool:
        return (
            total_requests_made < request_budget
            and source_stats[src_name]["requests_made"] < PER_SOURCE_REQUEST_CAP
            and not source_stats[src_name].get("fused")
        )

    def record(src_name: str, status: str, err_str: str, calls: int) -> None:
        nonlocal total_requests_made
        total_requests_made += calls
        source_stats[src_name]["requests_made"] += calls
        err_type = _error_class_from_str(err_str) if err_str else status
        if status in FUSABLE_STATUSES:
            if err_type == last_error_type_map.get(src_name, ""):
                consecutive_error_map[src_name] = consecutive_error_map.get(src_name, 0) + 1
            else:
                consecutive_error_map[src_name] = 1
            last_error_type_map[src_name] = err_type
            source_stats[src_name]["last_error_type"] = err_type
            source_stats[src_name]["consecutive_failures"] = consecutive_error_map[src_name]
            if consecutive_error_map[src_name] >= 3:
                prior = source_stats[src_name]
                fused = _fuse_source(
                    src_name, prior["label"],
                    f"3 consecutive failures: {err_type}",
                )
                fused["mode"] = prior.get("mode", "")
                fused["requests_made"] = prior.get("requests_made", 0)
                fused["consecutive_failures"] = consecutive_error_map[src_name]
                fused["last_error_type"] = err_type
                source_stats[src_name] = fused
        else:
            consecutive_error_map[src_name] = 0
            source_stats[src_name]["consecutive_failures"] = 0

    # ---- batch sources (one fetch per source) ----
    if not batch_completed:
        for src in SOURCE_DEFINITIONS:
            if src["mode"] != "batch":
                continue
            src_name = src["name"]
            if not budget_ok(src_name):
                break
            if time.monotonic() >= deadline:
                break
            results, err, calls = _probe_batch_source(
                ak_module, src, requests, req_counter, request_ledger,
            )
            all_results.extend(results)
            status = _classify_probe_error(err)[0] if err else STATUS_PROVEN
            record(src_name, status, err or "", calls)
            log.info("  batch %s: %d calls", src_name, calls)
        batch_completed = True
        save_checkpoint()

    # ---- per-code sources ----
    for code in unique_codes:
        if code in completed_codes:
            continue
        if time.monotonic() >= deadline:
            log.warning("Deadline reached (%d min), stopping probe", max_time_minutes)
            break
        req = code_to_req[code]

        for src in SOURCE_DEFINITIONS:
            src_name = src["name"]
            stats = source_stats[src_name]
            if stats["fused"]:
                continue
            if src["mode"] == "batch":
                continue
            if time.monotonic() >= deadline:
                break

            if src["mode"] in ("per_code_history", "per_code_snapshot"):
                operation = f"{code}|{src_name}"
                if operation in completed_operations:
                    continue
                if not budget_ok(src_name):
                    continue
                if src["mode"] == "per_code_history":
                    results, calls = _probe_value_em(
                        ak_module, req, src, req_counter, request_ledger,
                    )
                else:
                    results, calls = _probe_valuation_comparison(
                        ak_module, req, src, req_counter, request_ledger,
                    )
                all_results.extend(results)
                fused_status = next((r.status for r in results if r.status in FUSABLE_STATUSES), STATUS_PROVEN)
                err_str = next((r.error for r in results if r.error), "")
                record(src_name, fused_status, err_str, calls)
                completed_operations.add(operation)
                save_checkpoint()
                log.info("  %s %s: %d calls", src_name, code, calls)

            elif src["mode"] == "per_code_per_indicator":
                for field, field_def in src["fields"].items():
                    operation = f"{code}|{src_name}|{field}"
                    if operation in completed_operations:
                        continue
                    if not budget_ok(src_name):
                        break
                    if time.monotonic() >= deadline:
                        break
                    results, calls = _probe_valuation_baidu(
                        ak_module, req, field, src, req_counter, request_ledger,
                    )
                    all_results.extend(results)
                    fused_status = next(
                        (r.status for r in results if r.status in FUSABLE_STATUSES),
                        STATUS_PROVEN,
                    )
                    err_str = next((r.error for r in results if r.error), "")
                    record(src_name, fused_status, err_str, calls)
                    completed_operations.add(operation)
                    save_checkpoint()
        expected_operations = {
            f"{code}|{src['name']}"
            for src in SOURCE_DEFINITIONS
            if src["mode"] in ("per_code_history", "per_code_snapshot")
            and not source_stats[src["name"]].get("fused")
        }
        expected_operations.update({
            f"{code}|{src['name']}|{field}"
            for src in SOURCE_DEFINITIONS
            if src["mode"] == "per_code_per_indicator"
            and not source_stats[src["name"]].get("fused")
            for field in src["fields"]
        })
        if expected_operations <= completed_operations:
            completed_codes.add(code)
        save_checkpoint()

    # dividend_yield: no source exposes a historical series -> FIELD_UNAVAILABLE
    # observation per code (provenance recorded as local assessment).
    dy_sha = sha256_text("no_source_dividend_yield_field_unavailable")
    existing_observation_ids = {r.observation_id for r in all_results}
    for code in unique_codes:
        observation_id = f"obs-local-{code}-dividend_yield"
        if observation_id in existing_observation_ids:
            continue
        req = code_to_req[code]
        all_results.append(ProbeResult(
            request_id=f"local-{next(req_counter):04d}",
            code_requested=code, code_resolved=code,
            code_type=req.code_type, name=req.name,
            source="", field="dividend_yield", value=None, unit="",
            is_historical_series=False,
            source_title_or_endpoint="local_assessment",
            response_sha256=dy_sha, retrieved_at=now_iso(),
            status=STATUS_FIELD_UNAVAILABLE,
            error="no clear historical source for BJ dividend yield in this work order",
            observation_id=observation_id,
        ))
    save_checkpoint()

    ledger_count = sum(
        1 for row in request_ledger if row.get("counted_as_real_request")
    )
    if ledger_count != total_requests_made:
        raise RuntimeError(
            f"request ledger mismatch: ledger={ledger_count}, runtime={total_requests_made}"
        )
    request_ids = {row["request_id"] for row in request_ledger}
    missing_request_ids = sorted({
        r.request_id for r in all_results if r.source and r.request_id not in request_ids
    })
    if missing_request_ids:
        raise RuntimeError(
            f"observations reference missing request ids: {missing_request_ids[:5]}"
        )
    write_json(output_dir / "request-ledger.json", request_ledger)
    write_csv(output_dir / "request-ledger.csv", request_ledger)
    log.info("  total real requests made: %d (budget %d)", total_requests_made, request_budget)
    return all_results, list(source_stats.values())


def _write_source_capability_matrix(results: list[ProbeResult], source_stats: list[dict], output_dir: Path) -> None:
    lines = [
        "# Source Capability Matrix — BJ Valuation Fields (P2-07B)",
        "",
        f"Generated: {now_iso()}",
        "",
        "## Legend",
        "",
        "| Status | Meaning |",
        "|--------|---------|",
        "| PROVEN | >=3 successful observations (different codes or time points) |",
        "| LIMITED_SAMPLE | 1-2 successful observations, or mixed |",
        "| FIELD_UNAVAILABLE | Source does not expose the field for the probed codes |",
        "| CODE_NOT_FOUND | Probed codes not found in the source response |",
        "| ADAPTER_ERROR | Source adapter error (missing function, bad kwargs, parse failure, schema drift) |",
        "| NETWORK_ERROR | Network-level failure (connection/timeout/remote disconnect) |",
        "| RATE_LIMITED | Provider rate limiting / frequency control |",
        "| UNPROVEN | No data collected (not probed, or no mapping) |",
        "",
        "## Per-Source Field Capability",
        "",
        "| Source | Label | Mode | Fused |",
        "|--------|-------|------|-------|",
    ]

    for stat in source_stats:
        lines.append(f"| {stat['source']} | {stat['label']} | {stat.get('mode','')} | {'YES' if stat.get('fused') else 'no'} |")

    lines.append("")
    lines.append("## Per-Field Classification (across all sources)")
    lines.append("")
    lines.append("| Field | Classification | Notes |")
    lines.append("|------|---------------|-------|")

    for field in TARGET_FIELDS:
        classification = _classify_field(results, field)
        notes = ""
        for src in SOURCE_DEFINITIONS:
            if field in src["fields"]:
                note_key = "ps_note" if field == "ps_ttm" else ""
                if note_key and src.get(note_key):
                    notes += f"{src['label']}: {src[note_key]}; "
            else:
                if src["name"] not in [s["source"] for s in source_stats if s.get("fused")]:
                    notes += f"{src['label']}: field not exposed; "
        if field == "dividend_yield":
            notes += "no clear historical source (FIELD_UNAVAILABLE by design); "
        lines.append(f"| {field} | {classification} | {notes.rstrip('; ')} |")

    lines.append("")
    lines.append("## Per-Source Per-Field Detail")
    lines.append("")
    lines.append("| Source | Field | Samples | Proven | Unavailable | Network-Err | Classification |")
    lines.append("|--------|-------|---------|--------|-------------|-------------|---------------|")

    for src in SOURCE_DEFINITIONS:
        src_name = src["name"]
        for field in TARGET_FIELDS:
            related = [r for r in results if r.source == src_name and r.field == field]
            if not related:
                continue
            proven = sum(1 for r in related if r.status == STATUS_PROVEN)
            unavailable = sum(1 for r in related if r.status in (STATUS_FIELD_UNAVAILABLE, STATUS_CODE_NOT_FOUND))
            net_err = sum(1 for r in related if r.status in (STATUS_NETWORK_ERROR, STATUS_RATE_LIMITED))
            cls = _classify_field(results, field)
            lines.append(f"| {src_name} | {field} | {len(related)} | {proven} | {unavailable} | {net_err} | {cls} |")

    lines.append("")
    lines.append("## Individual Probe Results")
    lines.append("")
    lines.append("| RequestId | Code | Type | Name | Source | Field | Value | Unit | AsOf | PeriodStart | PeriodEnd | HistSeries | SrcCol | Status | Latency(ms) | Error |")
    lines.append("|-----------|------|------|------|--------|-------|-------|------|------|-------------|-----------|------------|--------|--------|-------------|-------|")

    for r in results:
        val_str = f"{r.value:.4f}" if isinstance(r.value, float) else str(r.value) if r.value else ""
        lines.append(
            f"| {r.request_id} | {r.code_requested} | {r.code_type} | {r.name} | {r.source} "
            f"| {r.field} | {val_str} | {r.unit} | {r.as_of_date} | {r.period_start} "
            f"| {r.period_end} | {'yes' if r.is_historical_series else 'no'} "
            f"| {r.source_column} | {r.status} | {r.latency_ms:.0f} | {r.error} |"
        )

    md = "\n".join(lines) + "\n"
    path = output_dir / "source-capability-matrix.md"
    path.write_text(md, encoding="utf-8")
    log.info("  wrote %s", path)


def _write_cross_source_consistency(results: list[ProbeResult], output_dir: Path) -> None:
    """Compare same-code/field observations with explicit date semantics.

    An undated snapshot may only be aligned to the latest dated observation
    when both observations were retrieved on the same UTC calendar date. This
    avoids treating a nearby month as the same valuation date.
    """
    rows: list[dict] = []
    codes = sorted({r.code_requested for r in results if r.source})
    source_defs = {s["name"]: s for s in SOURCE_DEFINITIONS}

    def semantic(source: str, field: str) -> str:
        d = source_defs.get(source, {}).get("fields", {}).get(field, {})
        if field == "pe_ttm":
            return "PE_TTM"
        if field == "pb_mrq":
            return "PB_MRQ"
        if field == "ps_ttm":
            return "PS_TTM"
        return field.upper() + ":" + str(d.get("unit", ""))

    for code in codes:
        related = [r for r in results if r.code_requested == code
                   and r.status == STATUS_PROVEN and r.source]
        fields = sorted({r.field for r in related})
        for field in fields:
            by_source: dict[str, list[ProbeResult]] = {}
            for r in related:
                if r.field == field:
                    by_source.setdefault(r.source, []).append(r)
            names = sorted(by_source)
            for idx, sa in enumerate(names):
                for sb in names[idx + 1:]:
                    aligned: tuple[ProbeResult, ProbeResult, str, str] | None = None
                    for x in by_source[sa]:
                        for y in by_source[sb]:
                            if x.as_of_date and x.as_of_date == y.as_of_date:
                                aligned = (x, y, x.as_of_date, "exact_date")
                                break
                        if aligned:
                            break
                    if aligned is None:
                        dated_a = sorted(
                            (r for r in by_source[sa] if r.as_of_date),
                            key=lambda r: r.as_of_date,
                        )
                        dated_b = sorted(
                            (r for r in by_source[sb] if r.as_of_date),
                            key=lambda r: r.as_of_date,
                        )
                        if dated_a and not dated_b:
                            x, y = dated_a[-1], by_source[sb][-1]
                            retrieval_date = y.retrieved_at[:10]
                            if retrieval_date and x.as_of_date == retrieval_date:
                                aligned = (x, y, x.as_of_date, "snapshot_retrieved_same_date")
                        elif dated_b and not dated_a:
                            x, y = by_source[sa][-1], dated_b[-1]
                            retrieval_date = x.retrieved_at[:10]
                            if retrieval_date and y.as_of_date == retrieval_date:
                                aligned = (x, y, y.as_of_date, "snapshot_retrieved_same_date")
                    if aligned is None:
                        continue
                    x, y, comparison_date, alignment_rule = aligned
                    semantic_a = semantic(sa, field)
                    semantic_b = semantic(sb, field)
                    compatible = x.unit == y.unit and semantic_a == semantic_b
                    vx, vy = float(x.value), float(y.value)
                    abs_diff = abs(vx - vy) if compatible else None
                    denominator = max(abs(vx), abs(vy))
                    rel_diff = (abs_diff / denominator) if compatible and denominator else None
                    status = (
                        "INCOMPARABLE" if not compatible
                        else "SAMPLE_ALIGNED" if rel_diff is not None and rel_diff <= 0.02
                        else "SAMPLE_DIFFERENT"
                    )
                    rows.append({
                        "code": code,
                        "security_name": x.name or y.name,
                        "field": field,
                        "as_of_date": comparison_date,
                        "source_a": sa,
                        "source_b": sb,
                        "value_a": vx,
                        "value_b": vy,
                        "unit_a": x.unit,
                        "unit_b": y.unit,
                        "semantic_a": semantic_a,
                        "semantic_b": semantic_b,
                        "absolute_difference": round(abs_diff, 8) if abs_diff is not None else None,
                        "relative_difference": round(rel_diff, 8) if rel_diff is not None else None,
                        "comparison_status": status,
                        "alignment_rule": alignment_rule,
                        "reason": "compatible unit and field semantics" if compatible
                                  else "unit or field semantics are incompatible",
                        "request_id_a": x.request_id,
                        "request_id_b": y.request_id,
                        "sample_consistent_le_2pct": status == "SAMPLE_ALIGNED",
                    })

    write_json(output_dir / "cross-source-consistency.json", rows)
    write_csv(output_dir / "cross-source-consistency.csv", rows)
    lines = [
        "# Cross-Source Consistency (P2-07B-R1)", "",
        f"Generated: {now_iso()}", "",
        "Only exact trading dates, or an undated snapshot retrieved on the",
        "same date as the latest dated observation, are comparable.", "",
        "| Code | Field | Date | Source A | Source B | Value A | Value B | RelDiff | Status | Alignment |",
        "|------|-------|------|----------|----------|---------|---------|---------|--------|-----------|",
    ]
    for r in rows:
        lines.append(
            f"| {r['code']} | {r['field']} | {r['as_of_date']} | {r['source_a']} "
            f"| {r['source_b']} | {r['value_a']} | {r['value_b']} "
            f"| {r['relative_difference']} | {r['comparison_status']} "
            f"| {r['alignment_rule']} |"
        )
    (output_dir / "cross-source-consistency.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8",
    )
    log.info("  wrote cross-source-consistency.md/.json/.csv")


def phase2(
    mappings: list[CodeMapping],
    output_dir: Path,
    max_codes: int,
    max_requests: int,
    max_time_minutes: int,
    checkpoint_path: Path | None = None,
    resume: bool = False,
) -> None:
    network_enabled = os.environ.get(NETWORK_ENV_VAR, "").strip() in ("1", "true", "TRUE", "yes")

    if not network_enabled:
        log.error("NETWORK PROBE BLOCKED: set %s=1 to enable network access", NETWORK_ENV_VAR)
        log.error("Assertion: NETWORK_ACCESS_ATTEMPTED without env var")
        sys.exit(1)

    log.info("Phase 2: network probe of BJ valuation sources")
    log.info("  max_codes=%d, max_requests=%d (per-source cap %d, total cap %d), max_time=%dmin",
             max_codes, max_requests, PER_SOURCE_REQUEST_CAP, TOTAL_REQUEST_CAP, max_time_minutes)

    ak = _probe_import_akshare()
    if ak is None:
        log.error("akshare required for network probe")
        sys.exit(1)

    plan = _build_probe_plan(mappings, max_codes)
    log.info("  probe plan: %d codes selected", len(plan))

    plan_dicts = [asdict(p) for p in plan]
    plan_path = output_dir / "probe-plan.json"
    write_json(plan_path, plan_dicts)
    log.info("  wrote %s", plan_path)

    results, source_stats = _run_probe_requests(
        plan, ak, output_dir, max_requests, max_time_minutes,
        checkpoint_path=checkpoint_path, resume=resume,
    )
    log.info("  probe complete: %d results across %d sources", len(results), len(source_stats))

    result_dicts = [asdict(r) for r in results]
    res_json_path = output_dir / "probe-results.json"
    res_csv_path = output_dir / "probe-results.csv"
    write_json(res_json_path, result_dicts)
    write_csv(res_csv_path, result_dicts)
    log.info("  wrote %s", res_json_path)
    log.info("  wrote %s", res_csv_path)

    _write_source_capability_matrix(results, source_stats, output_dir)
    _write_cross_source_consistency(results, output_dir)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Probe BJ-stock valuation data sources for QuantStar P2-07B",
    )
    p.add_argument(
        "--market-db",
        default=str(Path(__file__).resolve().parent.parent / "data" / "qs.db"),
        help="Path to qs.db (default: ../data/qs.db relative to script)",
    )
    p.add_argument(
        "--output-dir",
        default=str(Path(__file__).resolve().parent.parent / "data" / "probe_bj"),
        help="Output directory (default: ../data/probe_bj relative to script)",
    )
    p.add_argument(
        "--official-evidence",
        default="",
        help="Optional JSON list of complete BSE official old/new-code evidence records",
    )
    p.add_argument(
        "--probe",
        action="store_true",
        help="Enable network probe phase (requires %s=1 env var)" % NETWORK_ENV_VAR,
    )
    p.add_argument(
        "--max-codes",
        type=int,
        default=MAX_CODES_DEFAULT,
        help=f"Max codes to probe (default: {MAX_CODES_DEFAULT})",
    )
    p.add_argument(
        "--max-requests",
        type=int,
        default=TOTAL_REQUEST_CAP,
        help=f"Max total network requests (default: {TOTAL_REQUEST_CAP})",
    )
    p.add_argument(
        "--max-time-minutes",
        type=int,
        default=MAX_TIME_MINUTES_DEFAULT,
        help=f"Max total probe time in minutes (default: {MAX_TIME_MINUTES_DEFAULT})",
    )
    p.add_argument(
        "--checkpoint",
        default="",
        help="Optional checkpoint JSON path for crash-safe probe resume",
    )
    p.add_argument(
        "--resume",
        action="store_true",
        help="Resume from --checkpoint; fails if the checkpoint or plan is invalid",
    )
    return p.parse_args(argv)


def main() -> None:
    args = parse_args()

    market_db = Path(args.market_db).resolve()
    if not market_db.is_file():
        log.error("market DB not found: %s", market_db)
        sys.exit(1)

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    official_evidence: list[dict] = []
    if args.official_evidence:
        evidence_path = Path(args.official_evidence).resolve()
        if not evidence_path.is_file():
            log.error("official evidence not found: %s", evidence_path)
            sys.exit(1)
        try:
            loaded = json.loads(evidence_path.read_text(encoding="utf-8"))
            if not isinstance(loaded, list):
                raise ValueError("official evidence root must be a JSON list")
            official_evidence = loaded
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            log.error("invalid official evidence: %s", exc)
            sys.exit(1)
    mappings = phase1(market_db, output_dir, official_evidence)

    if args.resume and not args.checkpoint:
        log.error("--resume requires --checkpoint")
        sys.exit(1)
    checkpoint_path = Path(args.checkpoint).resolve() if args.checkpoint else None

    if args.probe:
        phase2(
            mappings, output_dir, args.max_codes, args.max_requests,
            args.max_time_minutes, checkpoint_path=checkpoint_path,
            resume=args.resume,
        )
    else:
        log.info("Phase 2 skipped (use --probe to enable network probe)")

    log.info("Done.")


if __name__ == "__main__":
    main()
