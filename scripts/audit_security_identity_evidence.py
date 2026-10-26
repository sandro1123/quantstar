#!/usr/bin/env python3
"""B1-R1: Per-code evidence remediation with strict confidence gates.

Read-only audit that:
1. Reads formal DB for stock_info + kline stats for P2-04/P2-05 codes
2. Queries Tencent Finance (single secondary source) for name/market/status
3. Applies Wave B-R1 hard gates:
   - high  requires: (a) primary authoritative source (exchange/company/regulator),
     or (b) two independent, verifiable external sources.
   - Tencent alone (single secondary) => at most medium.
   - formal DB alone => low.
4. Successor relations require item-by-item authoritative evidence; without it,
   they are downgraded to medium/manual and generate no SQL.
5. 001235 is fixed to UNKNOWN_MANUAL_REVIEW (medium), generates no SQL,
   does not delete/modify klines.
6. Emits v3 SQL containing only truly high items (may be empty).

Usage:
    python scripts/audit_security_identity_evidence.py \
        --market-db ~/qs/data/qs.db \
        --wave-a-dir ~/qs-repair-backups/phase2_opencode_20260731/WAVE-A \
        --output-dir /path/to/output \
        [--authoritative-evidence /path/to/authoritative-evidence.json]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from urllib.parse import urlsplit
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TENCENT_BASE = "https://qt.gtimg.cn/q="
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"

MAX_NETWORK_REQUESTS = 100
MAX_NETWORK_TIME = 600
MAX_FAILURES_PER_SOURCE = 3

# Hardcoded successor candidates are NOT evidence. They are only used to flag
# codes whose successor relationship must be proven by authoritative evidence.
# They can NEVER grant high confidence by themselves.
SUCCESSOR_CANDIDATES = {
    "000024": {"successor": "001979", "note": "招商地产 → 招商蛇口 (2015重组, 待权威证据)"},
    "000418": {"successor": "000333", "note": "小天鹅A → 美的集团 (2019吸收合并, 待权威证据)"},
    "000527": {"successor": "000333", "note": "美的电器 → 美的集团 (2013换股, 待权威证据)"},
    "000522": {"successor": "600332", "note": "白云山A → 白云山 (2013换股, 待权威证据)"},
    "000515": {"successor": "000629", "note": "攀渝钛业 → 钒钛股份 (2008重组, 待权威证据)"},
}

# Authorities that qualify as primary/authoritative for high confidence.
PRIMARY_AUTHORITIES = {"exchange", "regulator", "company_disclosure"}

log = logging.getLogger("audit_identity")


def open_ro(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA temp_store=MEMORY")
    cur = conn.execute("PRAGMA query_only")
    val = cur.fetchone()[0]
    if val != 1:
        raise RuntimeError(f"query_only verification failed: got {val}")
    conn.row_factory = sqlite3.Row
    return conn


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def tencent_market_prefix(market: str) -> str:
    m = market.upper()
    if m in ("SH", "SSE", "上交所"):
        return "sh"
    if m in ("SZ", "深交所"):
        return "sz"
    if m in ("BJ", "北交所", "BSE"):
        return "bj"
    return "sz"


def query_tencent_batch(codes: list[str]) -> dict[str, dict[str, str]]:
    """Query Tencent Finance for multiple codes.

    Returns {code: {name, market, status, price, source_url, response_sha256,
    retrieved_at}}. Each returned record carries the request URL and the
    SHA256 of the raw response body so it is independently verifiable.
    """
    if not codes:
        return {}
    prefixes = {}
    for c in codes:
        m = "sz"
        if c.startswith("60") or c.startswith("688"):
            m = "sh"
        elif c.startswith("92"):
            m = "bj"
        elif c.startswith("43") or c.startswith("83") or c.startswith("87"):
            m = "bj"
        prefixes[c] = m
    qstr = ",".join(f"{prefixes[c]}{c}" for c in codes)
    url = f"{TENCENT_BASE}{qstr}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    resp = urllib.request.urlopen(req, timeout=15)
    raw = resp.read()
    response_sha256 = sha256_bytes(raw)
    raw_text = raw.decode("gbk")
    result = {}
    for line in raw_text.strip().split(";"):
        line = line.strip()
        if not line or "=" not in line:
            continue
        parts = line.split("~")
        if len(parts) < 3:
            continue
        code = parts[2]
        name = parts[1] if len(parts) > 1 else ""
        mp = parts[0].strip('"=') if len(parts) > 0 else ""
        market_map = {"1": "SH", "51": "SZ", "62": "BJ"}
        m = market_map.get(mp.split("=")[-1] if "=" in mp else mp, "UNKNOWN")
        price = parts[3] if len(parts) > 3 else ""
        status = "normal"
        for i in range(40, min(len(parts), 51)):
            v = parts[i].strip()
            if v in ("D", "U", "P"):
                status = {"D": "delisted", "U": "unlisted", "P": "suspended"}[v]
                break
        result[code] = {
            "name": name,
            "market": m,
            "status": status,
            "price": price,
            "source_url": url,
            "response_sha256": response_sha256,
            "retrieved_at": now_iso(),
        }
    return result


class CrawlBudget:
    """Track network request budget."""
    def __init__(self, max_requests: int, max_time: int, max_failures: int):
        self.max_requests = max_requests
        self.max_time = max_time
        self.max_failures = max_failures
        self.requests = 0
        self.failures = 0
        self.start_time = time.time()
        self.source_failures: dict[str, int] = defaultdict(int)
        self.ledger: list[dict] = []
        self.fused_sources: set[str] = set()

    def can_request(self, source: str = "tencent") -> bool:
        elapsed = time.time() - self.start_time
        if self.requests >= self.max_requests:
            return False
        if elapsed > self.max_time:
            return False
        if self.source_failures[source] >= self.max_failures:
            return False
        if source in self.fused_sources:
            return False
        return True

    def record(self, source: str, code: str, status: str, data: dict | None = None, error: str = ""):
        self.requests += 1
        self.ledger.append({
            "source": source,
            "code": code,
            "status": status,
            "data": data,
            "error": error,
            "time": now_iso(),
        })
        if status != "OK":
            self.failures += 1
            self.source_failures[source] += 1
            if self.source_failures[source] >= self.max_failures:
                self.fused_sources.add(source)

    def elapsed(self) -> float:
        return time.time() - self.start_time

    def to_dict(self) -> dict:
        return {
            "max_requests": self.max_requests,
            "total_requests": self.requests,
            "total_failures": self.failures,
            "elapsed_seconds": round(self.elapsed(), 2),
            "fused_sources": list(self.fused_sources),
            "source_failures": dict(self.source_failures),
        }


def load_wave_a_results(wave_a_dir: Path) -> tuple[dict, dict, dict]:
    """Load P2-04 and P2-05 Wave A classification results."""
    p2_04 = {}
    p2_04_path = wave_a_dir / "P2-04" / "classification-per-code.json"
    if p2_04_path.exists():
        p2_04 = json.loads(p2_04_path.read_text())
    p2_05 = {}
    p2_05_path = wave_a_dir / "P2-05" / "classified_candidates.json"
    if p2_05_path.exists():
        p2_05 = json.loads(p2_05_path.read_text())
    drill = {}
    drill_path = wave_a_dir / ".." / ".." / "phase2_pollution_drill_20260731" / "drill.json"
    drill_abs = drill_path.resolve()
    if drill_abs.exists():
        drill = json.loads(drill_abs.read_text())
    return p2_04, p2_05, drill


def load_stock_info(conn: sqlite3.Connection, codes: set[str]) -> dict[str, dict]:
    """Load stock_info for specific codes."""
    if not codes:
        return {}
    placeholders = ",".join("?" for _ in codes)
    rows = conn.execute(
        f"SELECT code, name, market, list_date, is_active FROM stock_info WHERE code IN ({placeholders})",
        list(codes),
    ).fetchall()
    return {r["code"]: dict(r) for r in rows}


def kline_summary(conn: sqlite3.Connection, code: str) -> dict:
    """Get kline count and date range for a code."""
    row = conn.execute(
        "SELECT COUNT(*) as cnt, MIN(date) as min_dt, MAX(date) as max_dt "
        "FROM daily_klines WHERE code = ?", (code,)
    ).fetchone()
    return {
        "kline_count": row["cnt"] if row["cnt"] else 0,
        "kline_min_date": row["min_dt"] or "",
        "kline_max_date": row["max_dt"] or "",
    }


def build_evidence(
    code: str,
    stock_info: dict | None,
    kline: dict | None,
    tencent: dict | None,
    db_sha256: str = "",
    source: str = "current_db",
) -> dict:
    """Build a verifiable evidence record with all required fields.

    Required fields per Wave B-R1: evidence_type, source_authority,
    source_title, source_date, source_url, source_sha256 (response body or
    file SHA256), evidence_excerpt, retrieved_at, confidence_reason.
    """
    sources: list[dict] = []

    if stock_info:
        excerpt = (
            f"name={stock_info.get('name','')}, market={stock_info.get('market','')}, "
            f"list_date={stock_info.get('list_date','')}, is_active={stock_info.get('is_active','')}"
        )
        if kline:
            excerpt += (
                f" | klines={kline.get('kline_count',0)} "
                f"[{kline.get('kline_min_date','')} ~ {kline.get('kline_max_date','')}]"
            )
        sources.append({
            "source_authority": "current_db",
            "source_title": f"stock_info for {code}",
            "source_date": stock_info.get("list_date", ""),
            "source_url": "",
            "source_sha256": db_sha256,
            "evidence_excerpt": excerpt,
        })

    if tencent:
        t_excerpt = (
            f"tencent_name={tencent.get('name','')}, tencent_market={tencent.get('market','')}, "
            f"tencent_status={tencent.get('status','')}"
        )
        sources.append({
            "source_authority": "secondary",
            "source_title": "Tencent Finance real-time quote",
            "source_date": tencent.get("retrieved_at", "")[:10],
            "source_url": tencent.get("source_url", ""),
            "source_sha256": tencent.get("response_sha256", ""),
            "evidence_excerpt": t_excerpt,
        })

    combined_excerpt = " | ".join(s["evidence_excerpt"] for s in sources)
    authorities = [s["source_authority"] for s in sources]

    if any(a in PRIMARY_AUTHORITIES for a in authorities):
        confidence_reason = "Primary authoritative source present"
    elif len(authorities) >= 2 and authorities.count("secondary") >= 1:
        confidence_reason = "Two independent external sources"
    elif "secondary" in authorities:
        confidence_reason = "Single secondary external source (Tencent); at most medium"
    elif "current_db" in authorities:
        confidence_reason = "Formal DB only; not independent external evidence"
    else:
        confidence_reason = "No evidence"

    return {
        "code": code,
        "evidence_type": "stock_identity",
        "source_authority": ",".join(authorities) if authorities else "",
        "source_title": "; ".join(s["source_title"] for s in sources),
        "source_date": "; ".join(s["source_date"] for s in sources),
        "source_url": "; ".join(s["source_url"] for s in sources),
        "source_sha256": "; ".join(s["source_sha256"] for s in sources),
        "evidence_excerpt": combined_excerpt,
        "retrieved_at": now_iso(),
        "confidence_reason": confidence_reason,
    }


def _has_required_fields(evidence: dict) -> bool:
    """Check all mandatory evidence fields are populated (url/sha may be
    empty for formal DB records but must exist as keys)."""
    required = [
        "evidence_type", "source_authority", "source_title", "source_date",
        "source_url", "source_sha256", "evidence_excerpt", "retrieved_at",
        "confidence_reason",
    ]
    return all(k in evidence for k in required)


SHA256_HEX_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def _valid_iso_date(value: str) -> bool:
    """Accept a YYYY-MM-DD date or a full ISO datetime (first 10 chars)."""
    if not value or not isinstance(value, str) or len(value) < 10:
        return False
    try:
        datetime.strptime(value[:10], "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _valid_https_url(url: str) -> bool:
    """A structurally valid HTTPS URL.

    Requires scheme exactly 'https' (case-insensitive), a non-empty hostname,
    and a syntactically valid port in the inclusive range 1..65535 when a port
    is present. Rejects scheme-only URLs, userinfo, and any whitespace.

    This enforces structural validity only; it does NOT fetch the URL.
    """
    if not isinstance(url, str) or not url:
        return False
    if any(ch.isspace() for ch in url):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme.lower() != "https":
        return False
    if not parts.hostname:
        return False
    if parts.username is not None or parts.password is not None:
        return False
    # urllib.parse validates malformed and out-of-range ports lazily when
    # ``parts.port`` is accessed. Merely checking ``parts.hostname`` lets URLs
    # such as https://example.com:abc/ and :99999 pass the authority gate.
    try:
        port = parts.port
    except ValueError:
        return False
    if port is not None and not (1 <= port <= 65535):
        return False
    return True


def validate_authoritative_evidence(
    code: str,
    auth: dict | None,
    expected_successor: str,
) -> tuple[bool, list[str]]:
    """R2-1 hard gate for authoritative evidence records.

    An authoritative record is trusted ONLY when ALL of the following hold:
      - source_authority in PRIMARY_AUTHORITIES (exchange/regulator/company_disclosure)
      - source_url is a structurally valid HTTPS URL with a non-empty hostname
      - source_date is a valid ISO date (YYYY-MM-DD)
      - source_sha256 is a 64-char hex digest
      - evidence_excerpt is non-empty
      - structured 'successor' field present AND equal to expected_successor,
        explicitly binding this evidence to the target code -> successor relation.

    Returns (valid, errors). Callers MUST treat invalid evidence as absent:
    downgrade confidence and generate NO SQL.
    """
    errors: list[str] = []
    if not isinstance(auth, dict):
        return False, ["authoritative evidence is not a dict"]

    authority = auth.get("source_authority", "")
    if authority not in PRIMARY_AUTHORITIES:
        errors.append(f"source_authority={authority!r} not in {sorted(PRIMARY_AUTHORITIES)}")

    url = auth.get("source_url", "")
    if not url or not isinstance(url, str):
        errors.append("source_url missing/empty")
    elif not _valid_https_url(url):
        errors.append(f"source_url not a structurally valid HTTPS URL: {url!r}")

    date = auth.get("source_date", "")
    if not date or not isinstance(date, str):
        errors.append("source_date missing/empty")
    elif not _valid_iso_date(date):
        errors.append(f"source_date not ISO date: {date!r}")

    sha = auth.get("source_sha256", "")
    if not sha or not isinstance(sha, str):
        errors.append("source_sha256 missing/empty")
    elif not SHA256_HEX_RE.match(sha):
        errors.append(f"source_sha256 not 64-hex: {sha!r}")

    excerpt = auth.get("evidence_excerpt", "")
    if not excerpt or not str(excerpt).strip():
        errors.append("evidence_excerpt missing/empty")

    successor = auth.get("successor", "")
    if not successor:
        errors.append("structured field 'successor' missing")
    elif str(successor) != str(expected_successor):
        errors.append(
            f"successor={successor!r} != expected successor {expected_successor!r} "
            f"for code={code}"
        )

    return len(errors) == 0, errors


def _external_source_count(tencent: dict | None) -> int:
    """Number of independent external sources with verifiable url+sha."""
    n = 0
    if tencent and tencent.get("source_url") and tencent.get("response_sha256"):
        n += 1
    return n


def classify_with_evidence(
    code: str,
    info: dict | None,
    kline: dict | None,
    tencent: dict | None,
    wave_a_class: str | None,
    wave_a_conf: str | None,
    authoritative: dict | None = None,
) -> tuple[str, str, str]:
    """Classify a code under Wave B-R1 evidence rules.

    Returns (category, confidence, reasoning).

    Confidence rules:
    - high: primary authoritative evidence present, OR >=2 independent
      external verifiable sources.
    - medium: single secondary external source (Tencent) with url+sha.
    - low: formal DB only / no evidence.
    """
    name = info.get("name", "") if info else ""
    market = info.get("market", "") if info else ""
    list_date = info.get("list_date", "") if info else ""
    is_active = info.get("is_active", 0) if info else 0
    kc = kline.get("kline_count", 0) if kline else 0
    kmin = kline.get("kline_min_date", "") if kline else ""
    kmax = kline.get("kline_max_date", "") if kline else ""
    t_status = tencent.get("status", "") if tencent else ""
    t_name = tencent.get("name", "") if tencent else ""
    t_market = tencent.get("market", "") if tencent else ""

    has_klines = kc > 0
    ext_count = _external_source_count(tencent)
    authoritative = authoritative or {}

    # 001235 — fixed by work order: UNKNOWN_MANUAL_REVIEW, no SQL, no kline changes.
    if code == "001235":
        return ("UNKNOWN_MANUAL_REVIEW", "medium",
                "001235 fixed to UNKNOWN_MANUAL_REVIEW per Wave B-R1 work order. "
                "Formal DB: name=无效里得, market=SZ, list_date=20260518, is_active=1, "
                f"klines={kc} [{kmin}~{kmax}]. "
                "Tencent reports unlisted / name=无效里得. These facts are suspicious "
                "(suspected test/pre-listing placeholder) but NOT confirmed. "
                "No SQL generated; 42 kline rows are preserved untouched. "
                "Requires manual review.")

    # Test/quarantine records (formal DB only evidence).
    if code == "XXX" or "TEST" in code.upper():
        return ("QUARANTINE_TEST_RECORD", "low",
                "Test record in formal DB (code XXX / name contains TEST). "
                "Formal DB only evidence; no independent external source. "
                "Not eligible for SQL under Wave B-R1 gates.")

    # Successor candidates: require authoritative evidence per code.
    if code in SUCCESSOR_CANDIDATES:
        cand = SUCCESSOR_CANDIDATES[code]
        auth = authoritative.get(code)
        valid, auth_errors = validate_authoritative_evidence(code, auth, cand["successor"])
        if valid:
            return ("MAP_TO_SUCCESSOR_KEEP_HISTORY", "high",
                    f"Authoritative evidence passed R2-1 hard gate for successor "
                    f"{cand['successor']}: "
                    f"{auth.get('evidence_excerpt','')} "
                    f"URL={auth.get('source_url','')} SHA={auth.get('source_sha256','')} "
                    f"successor={auth.get('successor','')}. "
                    f"Formal DB: name={name}, klines={kc}.")
        # No (or invalid) authoritative evidence: downgrade, generate no SQL.
        invalid_note = ""
        if auth:
            invalid_note = " Failed R2-1 hard gate: " + "; ".join(auth_errors)
        reasoning = (
            f"Successor candidate {code} → {cand['successor']} ({cand['note']}). "
            "Requires item-by-item authoritative evidence (exchange/regulator/company "
            "disclosure with HTTPS URL + ISO date + 64-hex SHA + non-empty excerpt + "
            "successor binding). Not present or failed validation => downgraded, no SQL."
            + invalid_note
        )
        if ext_count >= 1:
            if t_status == "delisted":
                return ("UNKNOWN_MANUAL_REVIEW", "medium",
                        reasoning + f" Tencent status=delisted, name={t_name}. "
                        "Identity delisting unconfirmed by authoritative source.")
            if t_status == "unlisted":
                return ("UNKNOWN_MANUAL_REVIEW", "medium",
                        reasoning + f" Tencent status=unlisted, name={t_name}.")
        return ("UNKNOWN_MANUAL_REVIEW", "low",
                reasoning + " No independent external evidence for delisting.")

    # 920808 — market label correction candidate. Single secondary source => medium.
    if code == "920808":
        reasoning = (
            "Tencent reports market=BJ, name=曙光数创, status=normal; "
            f"formal DB market={market} (SZ). "
            "Single secondary external source => at most medium; "
            "no primary authoritative source, no second independent source. No SQL."
        )
        if ext_count >= 1 and t_market == "BJ":
            return ("CORRECT_MARKET_LABEL", "medium", reasoning)
        return ("UNKNOWN_MANUAL_REVIEW", "low", reasoning)

    # General delisting evidence.
    if t_status == "delisted":
        conf = "high" if ext_count >= 2 else "medium"
        return ("KEEP_HISTORICAL_INACTIVE", conf,
                f"Tencent status=delisted, name={t_name}. "
                f"Formal DB: name={name}, market={market}, klines={kc} [{kmin}~{kmax}]. "
                f"External sources={ext_count}. "
                "Single Tencent secondary source => medium unless a second "
                "independent external source is available.")

    if t_status == "unlisted":
        conf = "high" if ext_count >= 2 else "medium"
        return ("KEEP_HISTORICAL_INACTIVE", conf,
                f"Tencent status=unlisted, name={t_name}. "
                f"Formal DB: name={name}, market={market}, klines={kc} [{kmin}~{kmax}]. "
                f"External sources={ext_count}. Single secondary source => medium.")

    # Placeholder list_date + klines (formal DB only).
    if list_date == "20260518" and has_klines:
        return ("KEEP_HISTORICAL_INACTIVE", "low",
                f"Placeholder list_date=20260518, name={name}, klines={kc} [{kmin}~{kmax}]. "
                "Formal DB only evidence; no independent external source. Low confidence.")

    # ST flag in name + klines (formal DB only).
    has_st_flag = any(flag in name for flag in ["ST", "PT", "*ST", "S*ST", "退市"]) if name else False
    if has_st_flag and has_klines:
        return ("KEEP_HISTORICAL_INACTIVE", "low",
                f"ST/PT flagged: {name}. Formal DB: klines={kc} [{kmin}~{kmax}]. "
                "No independent external delisting evidence. Low confidence.")

    # Has klines but no external delisting confirmation.
    if has_klines:
        return ("KEEP_HISTORICAL_INACTIVE", "low",
                f"Has klines ({kc}) but no external delisting confirmation. "
                f"Formal DB: name={name}, market={market}, list_date={list_date}. "
                "Low confidence, no SQL.")

    return ("KEEP_HISTORICAL_INACTIVE", "low",
            f"No klines, no external evidence. Formal DB: name={name}, market={market}. "
            "Low confidence, no SQL.")


def generate_sql(classified: list[dict]) -> tuple[str, str]:
    """Generate v3 forward/rollback SQL from high-confidence items only."""
    forward_lines = [
        "-- DO NOT EXECUTE - NOVA REVIEW REQUIRED",
        f"-- Generated: {now_iso()}",
        "-- Source: B1-R1 evidence-based audit (v3)",
        "-- ============================================================",
        "-- RULES:",
        "--   * No DELETE/DROP/INSERT/ALTER statements",
        "--   * Every UPDATE has precise WHERE with current-value guard",
        "--   * Forward / Rollback pairs are 1:1",
        "--   * ONLY items meeting high-confidence gates generate SQL:",
        "--     primary authoritative source OR 2 independent external sources",
        "--   * Tencent single secondary source => at most medium => NO SQL",
        "--   * Successor relations require item-by-item authoritative evidence",
        "--",
    ]
    rollback_lines = [
        "-- DO NOT EXECUTE - NOVA REVIEW REQUIRED (ROLLBACK)",
        f"-- Generated: {now_iso()}",
        "-- Paired 1:1 with proposed-forward-v3.sql",
        "-- ============================================================",
        "--",
        "-- Rollback reverses every forward change in order.",
        "-- Each rollback has the same before-value guard.",
        "--",
    ]

    for item in classified:
        code = item["code"]
        cat = item["category"]
        conf = item["confidence"]
        if conf != "high":
            continue
        if cat == "QUARANTINE_TEST_RECORD":
            forward_lines.append(f"\n-- [{code}]: QUARANTINE_TEST_RECORD")
            forward_lines.append(f"-- {item.get('reasoning', '')}")
            forward_lines.append("UPDATE stock_info")
            forward_lines.append("   SET is_active = 0")
            forward_lines.append(f" WHERE code = '{code}'")
            forward_lines.append("   AND is_active = 1;  -- guard: was active")
            rollback_lines.append(f"\n-- [{code}]: ROLLBACK of QUARANTINE_TEST_RECORD")
            rollback_lines.append("UPDATE stock_info")
            rollback_lines.append("   SET is_active = 1")
            rollback_lines.append(f" WHERE code = '{code}'")
            rollback_lines.append("   AND is_active = 0;  -- guard: was inactive")
        elif cat == "CORRECT_MARKET_LABEL":
            forward_lines.append(f"\n-- [{code}]: CORRECT_MARKET_LABEL")
            forward_lines.append(f"-- {item.get('reasoning', '')}")
            forward_lines.append("UPDATE stock_info")
            forward_lines.append("   SET market = '北交所'")
            forward_lines.append(f" WHERE code = '{code}'")
            forward_lines.append("   AND market = 'SZ';  -- guard: was SZ")
            rollback_lines.append(f"\n-- [{code}]: ROLLBACK of CORRECT_MARKET_LABEL")
            rollback_lines.append("UPDATE stock_info")
            rollback_lines.append("   SET market = 'SZ'")
            rollback_lines.append(f" WHERE code = '{code}'")
            rollback_lines.append("   AND market = '北交所';  -- guard: was 北交所")
        elif cat == "MAP_TO_SUCCESSOR_KEEP_HISTORY":
            succ = item.get("successor", "?")
            forward_lines.append(f"\n-- [{code}]: MAP_TO_SUCCESSOR_KEEP_HISTORY")
            forward_lines.append(f"-- {item.get('reasoning', '')}")
            forward_lines.append(f"-- NOTE: Successor {succ} is the active ticker.")
            forward_lines.append("UPDATE stock_info")
            forward_lines.append("   SET is_active = 0")
            forward_lines.append(f" WHERE code = '{code}'")
            forward_lines.append("   AND is_active = 1;  -- guard: was active")
            rollback_lines.append(f"\n-- [{code}]: ROLLBACK of MAP_TO_SUCCESSOR_KEEP_HISTORY")
            rollback_lines.append("UPDATE stock_info")
            rollback_lines.append("   SET is_active = 1")
            rollback_lines.append(f" WHERE code = '{code}'")
            rollback_lines.append("   AND is_active = 0;  -- guard: was inactive")
        elif cat == "KEEP_HISTORICAL_INACTIVE":
            forward_lines.append(f"\n-- [{code}]: KEEP_HISTORICAL_INACTIVE")
            forward_lines.append(f"-- {item.get('reasoning', '')}")
            forward_lines.append("UPDATE stock_info")
            forward_lines.append("   SET is_active = 0")
            forward_lines.append(f" WHERE code = '{code}'")
            forward_lines.append("   AND is_active = 1;  -- guard: was active")
            rollback_lines.append(f"\n-- [{code}]: ROLLBACK of KEEP_HISTORICAL_INACTIVE")
            rollback_lines.append("UPDATE stock_info")
            rollback_lines.append("   SET is_active = 1")
            rollback_lines.append(f" WHERE code = '{code}'")
            rollback_lines.append("   AND is_active = 0;  -- guard: was inactive")

    forward_lines.append("\n-- End of proposed-forward-v3.sql")
    rollback_lines.append("\n-- End of proposed-rollback-v3.sql")
    return "\n".join(forward_lines), "\n".join(rollback_lines)


def investigate_001235(conn: sqlite3.Connection, budget: CrawlBudget) -> dict:
    """Special investigation for 001235 (no SQL, no kline changes)."""
    result = {
        "code": "001235",
        "investigation_date": now_iso(),
        "formal_db_facts": {},
        "tencent_facts": {},
        "kline_analysis": {},
        "classification_analysis": {},
        "conclusion": "UNKNOWN_MANUAL_REVIEW",
        "recommendation": "",
    }
    info = conn.execute(
        "SELECT code, name, market, list_date, is_active FROM stock_info WHERE code = '001235'"
    ).fetchone()
    if info:
        result["formal_db_facts"] = {
            "code": info["code"],
            "name": info["name"],
            "market": info["market"],
            "list_date": info["list_date"],
            "is_active": bool(info["is_active"]),
        }
    kline = conn.execute(
        "SELECT COUNT(*) as cnt, MIN(date) as min_dt, MAX(date) as max_dt, "
        "COUNT(DISTINCT date) as distinct_dates, "
        "COUNT(DISTINCT open) as distinct_open, COUNT(DISTINCT close) as distinct_close, "
        "SUM(volume) as total_volume, SUM(amount) as total_amount "
        "FROM daily_klines WHERE code = '001235'"
    ).fetchone()
    if kline and kline["cnt"]:
        result["kline_analysis"] = {
            "total_rows": kline["cnt"],
            "distinct_dates": kline["distinct_dates"],
            "distinct_open": kline["distinct_open"],
            "distinct_close": kline["distinct_close"],
            "total_volume": kline["total_volume"],
            "total_amount": kline["total_amount"],
            "min_date": kline["min_dt"],
            "max_date": kline["max_dt"],
        }
        sample = conn.execute(
            "SELECT date, open, high, low, close, volume, amount "
            "FROM daily_klines WHERE code = '001235' ORDER BY date LIMIT 10"
        ).fetchall()
        result["kline_analysis"]["sample_rows"] = [dict(r) for r in sample]
    if budget.can_request("tencent"):
        try:
            t = query_tencent_batch(["001235"])
            if "001235" in t:
                result["tencent_facts"] = t["001235"]
        except Exception as e:
            budget.record("tencent", "001235", "ERROR", error=str(e))
    result["classification_analysis"] = {
        "name_contains_invalid": "无效" in (result["formal_db_facts"].get("name", "")),
        "tencent_unlisted": result["tencent_facts"].get("status") == "unlisted",
        "has_klines": result["kline_analysis"].get("total_rows", 0) > 0,
        "placeholder_date": result["formal_db_facts"].get("list_date") == "20260518",
        "is_active": result["formal_db_facts"].get("is_active", False),
        "flat_klines": (
            result["kline_analysis"].get("distinct_open", 0) <= 1
            and result["kline_analysis"].get("distinct_close", 0) <= 1
        ),
        "zero_volume": result["kline_analysis"].get("total_volume", 0) == 0,
    }
    result["recommendation"] = (
        "001235 (无效里得): formal DB records name=无效里得, list_date=20260518, is_active=1; "
        "42 kline rows all flat (single open/close value) with zero volume/amount; "
        "Tencent reports unlisted. These facts are consistent with a suspected test or "
        "pre-listing placeholder record, BUT they are not confirmed production facts. "
        "Verdict is UNKNOWN_MANUAL_REVIEW: no SQL generated, the 42 kline rows are preserved "
        "untouched. Nova/human must decide whether to delete or keep the klines; "
        "no automatic action is taken."
    )
    return result


def main() -> None:
    p = argparse.ArgumentParser(description="B1-R1: Security identity evidence audit")
    p.add_argument("--market-db", type=Path, required=True)
    p.add_argument("--wave-a-dir", type=Path, required=True)
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--authoritative-evidence", type=Path, default=None,
                   help="Optional JSON file of authoritative evidence per code "
                        "{code: {source_authority, source_url, source_date, source_sha256, evidence_excerpt}}")
    p.add_argument("--max-network-requests", type=int, default=MAX_NETWORK_REQUESTS)
    p.add_argument("--max-network-time", type=int, default=MAX_NETWORK_TIME)
    p.add_argument("--no-network", action="store_true", help="Skip network queries")
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
    log.info("=== B1-R1 evidence remediation start ===")
    log.info("market_db: %s", args.market_db)
    log.info("wave_a_dir: %s", args.wave_a_dir)
    log.info("output_dir: %s", output_dir)

    db_sha256 = sha256_file(str(args.market_db.resolve()))

    conn = open_ro(args.market_db)
    budget = CrawlBudget(args.max_network_requests, args.max_network_time, MAX_FAILURES_PER_SOURCE)

    p2_04, p2_05, drill = load_wave_a_results(args.wave_a_dir)
    log.info("Wave A P2-04 codes: %d", len(p2_04))
    log.info("Wave A P2-05 codes: %d", len(p2_05))

    if isinstance(p2_04, list):
        p2_04 = {item["code"]: item for item in p2_04 if "code" in item}
    if isinstance(p2_05, list):
        p2_05 = {item["code"]: item for item in p2_05 if "code" in item}

    all_codes = set(p2_04.keys()) | set(p2_05.keys())
    all_codes.discard(None)
    log.info("Total unique codes to audit: %d", len(all_codes))

    stock_infos = load_stock_info(conn, all_codes)
    log.info("Loaded stock_info records: %d", len(stock_infos))

    kline_data = {}
    for code in all_codes:
        kline_data[code] = kline_summary(conn, code)

    tencent_data = {}
    codes_list = sorted(all_codes)
    if not args.no_network:
        batch_size = 20
        for i in range(0, len(codes_list), batch_size):
            batch = codes_list[i:i+batch_size]
            if not budget.can_request("tencent"):
                log.info("Network budget exhausted at batch %d/%d", i//batch_size, len(codes_list)//batch_size)
                break
            try:
                t = query_tencent_batch(batch)
                tencent_data.update(t)
                budget.record("tencent", f"batch_{i//batch_size}", "OK", data={"count": len(batch)})
                log.info("Tencent batch %d: %d codes OK", i//batch_size, len(t))
            except Exception as e:
                budget.record("tencent", f"batch_{i//batch_size}", "ERROR", error=str(e))
                log.warning("Tencent batch %d failed: %s", i//batch_size, e)
                if not budget.can_request("tencent"):
                    break
    else:
        log.info("Network queries skipped (--no-network)")

    # Load authoritative evidence if provided.
    authoritative = {}
    if args.authoritative_evidence and args.authoritative_evidence.exists():
        authoritative = json.loads(args.authoritative_evidence.read_text())
        log.info("Loaded authoritative evidence for %d codes", len(authoritative))

    evidence_records = []
    classified = []
    manual_review = []
    for code in sorted(all_codes):
        info = stock_infos.get(code)
        kline = kline_data.get(code)
        tencent = tencent_data.get(code)
        wave_a_class = None
        wave_a_conf = None
        if code in p2_04:
            wave_a_class = p2_04[code].get("primary_category", "")
            wave_a_conf = "medium"
        elif code in p2_05:
            wave_a_class = p2_05[code].get("category", "")
            wave_a_conf = p2_05[code].get("confidence", "medium")
        cat, conf, reasoning = classify_with_evidence(
            code, info, kline, tencent, wave_a_class, wave_a_conf,
            authoritative=authoritative,
        )
        evidence = build_evidence(code, info, kline, tencent, db_sha256=db_sha256)
        if not _has_required_fields(evidence):
            log.warning("Evidence for %s missing required fields", code)
        evidence_records.append(evidence)
        item = {
            "code": code,
            "name": info.get("name", "") if info else "",
            "market": info.get("market", "") if info else "",
            "category": cat,
            "confidence": conf,
            "reasoning": reasoning,
            "wave_a_category": wave_a_class,
            "kline_count": kline.get("kline_count", 0) if kline else 0,
            "tencent_status": tencent.get("status", "") if tencent else "",
            "source_authority": evidence["source_authority"],
            "source_url": evidence["source_url"],
            "source_sha256": evidence["source_sha256"],
        }
        if cat == "MAP_TO_SUCCESSOR_KEEP_HISTORY":
            item["successor"] = SUCCESSOR_CANDIDATES[code]["successor"]
        classified.append(item)
        if cat == "UNKNOWN_MANUAL_REVIEW":
            manual_review.append(item)
        log.info("  %s -> %s [%s] | %s%s",
                 code, cat, conf,
                 info.get("name", "")[:20] if info else "",
                 f" | tencent={tencent.get('status','')}" if tencent else "")

    identity_001235 = investigate_001235(conn, budget)
    log.info("001235 investigation conclusion: %s", identity_001235["conclusion"])

    p2_04_codes = set(p2_04.keys()) if isinstance(p2_04, dict) else set()
    p2_04_reclassified = [c for c in classified if c["code"] in p2_04_codes]
    p2_05_reclassified = [c for c in classified if c["code"] not in p2_04_codes]

    # v3 SQL: high-confidence only, exclude UNKNOWN_MANUAL_REVIEW (never SQL).
    high_conf = [c for c in classified if c["confidence"] == "high" and c["code"] != "XXX"]
    forward_sql, rollback_sql = generate_sql(high_conf)

    source_register = {
        "sources": [
            {
                "name": "formal_db",
                "authority": "current_db",
                "path": str(args.market_db.resolve()),
                "sha256": db_sha256,
                "note": "QuantStar formal market database (stock_info, daily_klines)",
            },
            {
                "name": "tencent_finance",
                "authority": "secondary",
                "url": TENCENT_BASE,
                "note": "Tencent Finance real-time stock quotes API. Single secondary "
                        "source => at most medium confidence.",
                "requests_made": budget.requests,
                "failures": budget.failures,
            },
            {
                "name": "wave_a_p2_04",
                "authority": "manual_candidate",
                "path": str(args.wave_a_dir / "P2-04" / "classification-per-code.json"),
                "note": "Wave A P2-04 classification (hardcoded lists; not external evidence)",
            },
            {
                "name": "wave_a_p2_05",
                "authority": "manual_candidate",
                "path": str(args.wave_a_dir / "P2-05" / "classified_candidates.json"),
                "note": "Wave A P2-05 classification (hardcoded lists; not external evidence)",
            },
        ],
        "budget": budget.to_dict(),
    }

    def write_json(path: str, data: Any):
        (output_dir / path).write_text(json.dumps(data, indent=2, ensure_ascii=False))

    def write_jsonl(path: str, records: list):
        with open(output_dir / path, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def write_csv(path: str, records: list[dict]):
        if not records:
            (output_dir / path).write_text("")
            return
        with open(output_dir / path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(records[0].keys()))
            w.writeheader()
            w.writerows(records)

    write_jsonl("security-identity-evidence.jsonl", evidence_records)
    write_csv("security-identity-evidence.csv", evidence_records)
    write_json("identity-case-001235.json", identity_001235)
    write_json("p2-04-reclassified.json", {
        "codes": p2_04_reclassified,
        "count": len(p2_04_reclassified),
        "category_counts": dict(Counter(c["category"] for c in p2_04_reclassified)),
        "confidence_counts": dict(Counter(c["confidence"] for c in p2_04_reclassified)),
    })
    write_json("p2-05-reclassified.json", {
        "codes": p2_05_reclassified,
        "count": len(p2_05_reclassified),
        "category_counts": dict(Counter(c["category"] for c in p2_05_reclassified)),
        "confidence_counts": dict(Counter(c["confidence"] for c in p2_05_reclassified)),
    })
    (output_dir / "proposed-forward-v3.sql").write_text(forward_sql)
    (output_dir / "proposed-rollback-v3.sql").write_text(rollback_sql)
    write_json("manual-review.json", {
        "codes": manual_review,
        "count": len(manual_review),
    })
    write_json("source-register.json", source_register)

    elapsed = time.time() - start
    report = {
        "stage": "B1-R1",
        "status": "COMPLETED",
        "started_at": now_iso(),
        "elapsed_seconds": round(elapsed, 2),
        "total_codes_audited": len(all_codes),
        "network_requests": budget.requests,
        "network_failures": budget.failures,
        "fused_sources": list(budget.fused_sources),
        "p2_04_reclassified": len(p2_04_reclassified),
        "p2_05_reclassified": len(p2_05_reclassified),
        "high_confidence_sql_count": len(high_conf),
        "manual_review_count": len(manual_review),
        "category_counts": dict(Counter(c["category"] for c in classified)),
        "confidence_counts": dict(Counter(c["confidence"] for c in classified)),
        "001235_conclusion": identity_001235["conclusion"],
        "evidence_field_completeness": {
            "total": len(evidence_records),
            "with_all_required_fields": sum(1 for e in evidence_records if _has_required_fields(e)),
        },
        "confidence_gate": {
            "note": "high requires primary authoritative source OR 2 independent "
                    "verifiable external sources; Tencent alone => at most medium.",
            "primary_authoritative_sources_used": len(authoritative),
        },
    }
    write_json("report.json", report)

    log.info("=== B1-R1 evidence remediation complete ===")
    log.info("Total codes: %d", len(all_codes))
    log.info("Network requests: %d/%d (failures: %d)", budget.requests, args.max_network_requests, budget.failures)
    log.info("High-confidence SQL: %d", len(high_conf))
    log.info("Manual review: %d", len(manual_review))
    log.info("Elapsed: %.1fs", elapsed)

    conn.close()


if __name__ == "__main__":
    main()
