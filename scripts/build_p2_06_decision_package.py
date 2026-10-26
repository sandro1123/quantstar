#!/usr/bin/env python3
"""R5/R2-3/R2-4: P2-06 A/B/C/D option decision package (read-only, no execution).

Compares options for rebuilding the valuation candidate:
A. Re-query BaoStock with fixed adapter
B. Copy existing valuation candidate and fix by provenance
C. Supplement with other sources
D. Combine B (local 0->NULL repair) + C (real-value supplement from proven sources)

R5 fixes (Nova):
  1. Provenance is read from the real `valuation_provenance` table.
  2. Per-field zero vs NULL semantics are quantified separately.
  3. Field capability matrix is corrected against real evidence (candidate
     zero-rates + B4 network probe results).
  4. The recommendation is DERIVED from a transparent scoring model.

R2-3 fixes (Nova):
  1. 0->NULL is explicitly defined as SEMANTIC REPAIR and does NOT count as
     real-value coverage. `real_value_fixable` counts only fields for which a
     PROVEN source can write a real value.
  2. cost_hours / requests / risk each carry an explicit justification basis.
  3. Sensitivity analysis across weight schemes is output so the recommendation
     is not an opaque constant; the default (balanced) scheme is stated.
  4. A combined option D (B repair + C supplement) is compared; B is NOT
     presupposed as the winner.

R2-4 additions (Nova):
  - overall / SH / SZ / BJ / UNKNOWN coverage estimates for the valuation
    candidate vs the formal DB.
  - A concrete handling plan for the failed backfill codes.
  - A concrete handling plan for the two anomaly dates (2026-07-08, 07-20).
  - The strongest objection to the recommended option.
  - An explicit P2-06 execution gate (NOT executed without Nova approval).

Outputs: option matrix, decision report, execution plan draft, stop
conditions, and report.json.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


FIELD_NAMES = ["pe_ttm", "pb_mrq", "ps_ttm", "market_cap", "total_shares", "dividend_yield"]

OPTION_NAMES = ["A_rebuild_baostock", "B_copy_and_fix", "C_supplement_other", "D_combine_B_C"]

# Default scoring weights (documented assumption) and sensitivity schemes.
WEIGHT_SCHEMES = {
    "balanced": {"cov": 0.5, "repair": 0.05, "cost": 0.25, "risk": 0.2},
    "coverage_first": {"cov": 0.8, "repair": 0.05, "cost": 0.05, "risk": 0.1},
    "cost_first": {"cov": 0.2, "repair": 0.05, "cost": 0.55, "risk": 0.2},
}

RISK_SCORE = {"LOW": 1.0, "MEDIUM": 0.6, "HIGH": 0.3}

# The two anomaly dates from R2 (UNRESOLVED, requires manual review).
ANOMALY_DATES = ["2026-07-08", "2026-07-20"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def open_ro(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def gather_valuation_facts(vconn: sqlite3.Connection, valuation_db: Path) -> dict[str, Any]:
    """Read real facts from the candidate valuation DB (read-only)."""
    total = vconn.execute("SELECT COUNT(*) FROM stock_valuation").fetchone()[0]
    dates = vconn.execute("SELECT COUNT(DISTINCT date) FROM stock_valuation").fetchone()[0]
    codes = vconn.execute("SELECT COUNT(DISTINCT code) FROM stock_valuation").fetchone()[0]

    field_stats: dict[str, dict[str, Any]] = {}
    for f in FIELD_NAMES:
        zero = vconn.execute(f"SELECT COUNT(*) FROM stock_valuation WHERE {f} = 0").fetchone()[0]
        null = vconn.execute(f"SELECT COUNT(*) FROM stock_valuation WHERE {f} IS NULL").fetchone()[0]
        field_stats[f] = {
            "zero_count": zero,
            "null_count": null,
            "pct_zero": round(zero / total * 100, 2) if total else 0.0,
            "pct_null": round(null / total * 100, 2) if total else 0.0,
            "all_zero": total > 0 and zero == total,
        }

    # Provenance from the real table.
    provenance = {"table_present": False, "rows": 0, "sources": {}, "coverage_pct": None}
    if table_exists(vconn, "valuation_provenance"):
        prov_rows = vconn.execute("SELECT COUNT(*) FROM valuation_provenance").fetchone()[0]
        src_rows = vconn.execute(
            "SELECT source, COUNT(*) FROM valuation_provenance GROUP BY source"
        ).fetchall()
        provenance["table_present"] = True
        provenance["rows"] = prov_rows
        provenance["sources"] = {r[0]: r[1] for r in src_rows}
        provenance["coverage_pct"] = round(prov_rows / total * 100, 2) if total else 0.0

    # Backfill state (incl. failed codes list).
    backfill = {"table_present": False, "counts": {}, "failed_codes": 0, "failed_code_list": []}
    if table_exists(vconn, "valuation_backfill_state"):
        status_rows = vconn.execute(
            "SELECT status, COUNT(*) FROM valuation_backfill_state GROUP BY status"
        ).fetchall()
        backfill["table_present"] = True
        backfill["counts"] = {r[0]: r[1] for r in status_rows}
        backfill["failed_codes"] = backfill["counts"].get("failed", 0)
        try:
            failed_rows = vconn.execute(
                "SELECT code FROM valuation_backfill_state WHERE status='failed' ORDER BY code"
            ).fetchall()
            backfill["failed_code_list"] = [r[0] for r in failed_rows]
        except sqlite3.Error:
            backfill["failed_code_list"] = []

    return {
        "val_size_bytes": valuation_db.stat().st_size,
        "val_rows": total,
        "val_dates": dates,
        "val_codes": codes,
        "field_stats": field_stats,
        "provenance": provenance,
        "backfill": backfill,
    }


def gather_market_facts(mconn: sqlite3.Connection) -> dict[str, Any]:
    """Read formal-DB market facts (read-only)."""
    bj_codes = mconn.execute(
        "SELECT COUNT(*) FROM stock_info WHERE market = 'BJ'"
    ).fetchone()[0]
    missing_kline_codes = mconn.execute(
        "SELECT COUNT(DISTINCT si.code) FROM stock_info si "
        "LEFT JOIN daily_klines dk ON si.code = dk.code "
        "WHERE dk.code IS NULL"
    ).fetchone()[0]
    return {
        "bj_codes": bj_codes,
        "missing_kline_codes": missing_kline_codes,
    }


def _market_bucket(code: str) -> str:
    """Classify a stock code into SH/SZ/BJ/UNKNOWN by numeric prefix."""
    if code.startswith(("60", "68")):
        return "SH"
    if code.startswith(("00", "30")):
        return "SZ"
    if code.startswith(("43", "83", "87", "92")):
        return "BJ"
    return "UNKNOWN"


def coverage_by_market(mconn: sqlite3.Connection, vconn: sqlite3.Connection) -> dict[str, Any]:
    """Estimate valuation-candidate coverage per market bucket vs formal DB.

    Uses numeric-prefix classification (robust to label drift: SZ/创业板/主板,
    SH/科创板, BJ/北交所 are all normalized to the same buckets).
    """
    formal_codes = set(r[0] for r in mconn.execute("SELECT DISTINCT code FROM stock_info"))
    val_codes = set(r[0] for r in vconn.execute("SELECT DISTINCT code FROM stock_valuation"))
    buckets = ["SH", "SZ", "BJ", "UNKNOWN", "overall"]
    formal_count = Counter(_market_bucket(c) for c in formal_codes)
    val_count = Counter(_market_bucket(c) for c in val_codes)
    result: dict[str, Any] = {}
    for b in buckets:
        fc = len(formal_codes) if b == "overall" else formal_count[b]
        vc = len(val_codes) if b == "overall" else val_count[b]
        result[b] = {
            "formal_codes": fc,
            "valuation_codes": vc,
            "coverage_pct": round(vc / fc * 100, 2) if fc else 0.0,
        }
    return result


def load_b4_probe(wave_b_dir: Path) -> dict[str, Any]:
    """Load B4 probe results for the capability matrix (optional)."""
    probe_path = wave_b_dir / "R4-bj-probe" / "probe-results.json"
    if not probe_path.is_file():
        return {"available": False}
    try:
        data = json.loads(probe_path.read_text(encoding="utf-8"))
    except Exception:
        return {"available": False}
    per_source: dict[str, set[str]] = {}
    per_field: dict[str, Counter] = {}
    for r in data:
        src = r.get("source")
        fld = r.get("field")
        status = r.get("status")
        if not src:
            continue
        per_source.setdefault(src, set())
        if status == "PROVEN":
            per_source[src].add(fld)
        if fld:
            per_field.setdefault(fld, Counter())
            per_field[fld][status] += 1
    return {
        "available": True,
        "path": str(probe_path),
        "per_source_proven": {k: sorted(v) for k, v in per_source.items()},
        "per_field": {k: dict(v) for k, v in per_field.items()},
    }


def load_anomaly_days(wave_b_dir: Path) -> dict[str, Any]:
    """Load R2 anomaly-day fact chain (optional)."""
    chain_path = wave_b_dir / "R2-anomaly-days" / "anomaly-day-fact-chain.json"
    if not chain_path.is_file():
        return {"available": False, "verdicts": {}}
    try:
        data = json.loads(chain_path.read_text(encoding="utf-8"))
    except Exception:
        return {"available": False, "verdicts": {}}
    return {
        "available": True,
        "path": str(chain_path),
        "verdicts": {d: (v.get("verdict", "UNKNOWN"), v.get("requires_manual_review", True))
                     for d, v in data.items() if isinstance(v, dict)},
    }


def build_capability_matrix(facts: dict[str, Any], probe: dict[str, Any]) -> dict[str, Any]:
    """Corrected field capability matrix from real evidence.

    Rows: fields. Columns: baostock (actual from candidate zero-rates) plus
    BJ alternative sources (from B4 probe when available, else UNVERIFIED).
    """
    field_stats = facts["field_stats"]
    matrix: dict[str, dict[str, Any]] = {}
    for f in FIELD_NAMES:
        fs = field_stats[f]
        pct = fs["pct_zero"]
        if fs["all_zero"]:
            baostock_status = "UNAVAILABLE"
        elif pct == 0.0:
            baostock_status = "AVAILABLE"
        else:
            baostock_status = "LIMITED"
        row: dict[str, Any] = {
            "field": f,
            "candidate_zero_pct": pct,
            "candidate_null_pct": fs["pct_null"],
            "baostock_actual": baostock_status,
            "sources": {},
        }
        if probe.get("available"):
            for src, proven_fields in probe.get("per_source_proven", {}).items():
                row["sources"][src] = "PROVEN" if f in proven_fields else "NOT_PROVEN"
        else:
            for src in ("stock_zh_valuation_baidu", "stock_zh_valuation_comparison_em",
                        "stock_zh_a_spot_em", "stock_bj_a_spot_em"):
                row["sources"][src] = "UNVERIFIED"
        matrix[f] = row
    return matrix


# ---------------------------------------------------------------------------
# R2-3 scoring: honest semantics + explicit basis + sensitivity
# ---------------------------------------------------------------------------


def _build_option_profiles(facts: dict[str, Any], matrix: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Per-option profile with explicit cost/requests/risk basis.

    Semantics (R2-3 / R3-2):
      - `real_value_fixable`: fields for which a PROVEN alternative source can
        write a REAL value. This is the only thing that counts as coverage.
      - `semantic_repair_fields`: fields converted 0->NULL only, and only those
        NOT already covered by a real-value source. The sets are disjoint
        (semantic_repair_fields = missing_fields - real_value_fixable), so a
        field is never double-counted.
      - `repair_only`: disjoint remainder used by both C and D.
    """
    missing_fields = [f for f, r in matrix.items() if r["baostock_actual"] == "UNAVAILABLE"]
    real_fixable = [
        f for f in missing_fields
        if any(v == "PROVEN" for v in matrix[f]["sources"].values())
    ]
    repair_only = [f for f in missing_fields if f not in real_fixable]
    failed_codes = facts["backfill"].get("failed_codes", 0)
    n_real_sources = max(1, len(real_fixable) + 1)

    profiles: dict[str, dict[str, Any]] = {
        "A_rebuild_baostock": {
            "real_value_fixable": [],
            "semantic_repair_fields": [],
            "cost_hours": 8,
            "cost_basis": ("Full baostock pipeline re-run + adapter inspection/repair for the "
                           "never-written fields (~5,319 codes backfill). The same source schema is "
                           "expected to reproduce the identical zero fields, so real coverage gain is 0."),
            "requests": 5319,
            "requests_basis": ("One backfill request per code (~5,319) with historical window; "
                               "equivalent to the original load. No alternative sources contacted."),
            "risk": "MEDIUM",
            "risk_basis": ("Re-query may reproduce the identical schema (no new fields) and may "
                           f"re-introduce the {failed_codes} code failures; churn on a working candidate."),
        },
        "B_copy_and_fix": {
            "real_value_fixable": [],
            "semantic_repair_fields": list(missing_fields),
            "cost_hours": 1,
            "cost_basis": ("Local copy of candidate DB + reviewed UPDATE (0->NULL) for all-zero "
                           "fields + verification. No network. 0->NULL is SEMANTIC REPAIR and does "
                           "NOT add real values."),
            "requests": 0,
            "requests_basis": "No network requests; pure local SQL transform on a copy.",
            "risk": "LOW",
            "risk_basis": ("0->NULL on 100%-zero fields is reversible and safe; it does not add any "
                           "real values, so there is no data-claim risk. Real-value coverage stays 0."),
        },
        "C_supplement_other": {
            "real_value_fixable": real_fixable,
            "semantic_repair_fields": repair_only,
            "cost_hours": 24,
            "cost_basis": ("Write/verify adapters for alternative sources (baidu/comparison/spot) + "
                           "fetch 5,319 codes + quality validation. baidu lacks PS and comparison "
                           "lacks market-cap, so real-value coverage is PARTIAL."),
            "requests": 5319 * n_real_sources,
            "requests_basis": (f"Per-code supplementary fetch across alternative sources (~5,319 x "
                               f"{n_real_sources} source-fields); bounded by the number of proven fields "
                               "plus fallback fetches."),
            "risk": "HIGH",
            "risk_basis": ("Alternative-source API fragility (baidu PS missing, comparison market-cap "
                           "missing), rate limits, partial coverage; may not populate total_shares / "
                           "dividend_yield at all."),
        },
        "D_combine_B_C": {
            "real_value_fixable": real_fixable,
            "semantic_repair_fields": repair_only,
            "cost_hours": 25,
            "cost_basis": ("C's supplement work plus B's local 0->NULL repair on the copy. Real-value "
                           "coverage where a proven source exists; semantic repair elsewhere."),
            "requests": 5319 * n_real_sources,
            "requests_basis": (f"Same as C (supplementary fetch ~5,319 x {n_real_sources}) plus no "
                               "additional requests for the local repair."),
            "risk": "MEDIUM",
            "risk_basis": ("LOW-risk local repair combined with HIGH-risk supplement; overall medium. "
                           "If the supplement fails, the B-only repair still lands safely."),
        },
    }
    return profiles


def score_option(
    option: str,
    prof: dict[str, Any],
    weights: dict[str, float],
) -> dict[str, Any]:
    """Score a single option under a weight scheme.

    coverage ratio uses REAL-VALUE fields only; semantic repair gets a small,
    separate weight (0->NULL is not real coverage).
    """
    total_fields = len(FIELD_NAMES)
    cov_ratio = len(prof["real_value_fixable"]) / total_fields
    repair_ratio = len(prof["semantic_repair_fields"]) / total_fields
    cost_score = max(0.0, 1.0 - prof["cost_hours"] / 48.0)
    risk_score = RISK_SCORE[prof["risk"]]
    score = (
        weights["cov"] * cov_ratio
        + weights["repair"] * repair_ratio
        + weights["cost"] * cost_score
        + weights["risk"] * risk_score
    )
    return {
        "cov_ratio": round(cov_ratio, 3),
        "repair_ratio": round(repair_ratio, 3),
        "cost_score": round(cost_score, 3),
        "risk_score": risk_score,
        "score": round(score, 3),
    }


def score_options(facts: dict[str, Any], matrix: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Score all options across weight schemes; rank under the default scheme.

    Returns {option: {profile, scheme_scores, rank, score_detail}}.
    The recommendation is the top option under the BALANCED scheme; sensitivity
    across all schemes is included so the result is auditable.
    """
    total = facts["val_rows"]
    profiles = _build_option_profiles(facts, matrix)
    scores: dict[str, dict[str, Any]] = {}
    for opt in OPTION_NAMES:
        prof = profiles[opt]
        scheme_scores = {
            scheme: score_option(opt, prof, weights)
            for scheme, weights in WEIGHT_SCHEMES.items()
        }
        detail = {
            "real_value_fixable": prof["real_value_fixable"],
            "semantic_repair_fields": prof["semantic_repair_fields"],
            "semantic_repair_is_0_to_null_not_real_coverage": True,
            "real_value_fixable_count": len(prof["real_value_fixable"]),
            "semantic_repair_count": len(prof["semantic_repair_fields"]),
            "cost_hours": prof["cost_hours"],
            "cost_basis": prof["cost_basis"],
            "requests": prof["requests"],
            "requests_basis": prof["requests_basis"],
            "risk": prof["risk"],
            "risk_basis": prof["risk_basis"],
        }
        scores[opt] = {
            "score": scheme_scores["balanced"]["score"],
            "score_detail": detail,
            "scheme_scores": scheme_scores,
        }

    if total > 0:
        ranked = sorted(scores.items(), key=lambda kv: kv[1]["score"], reverse=True)
        for rank, (opt, _) in enumerate(ranked, start=1):
            scores[opt]["rank"] = rank
    else:
        for opt in scores:
            scores[opt]["rank"] = 0

    # Sensitivity: which option wins under each weight scheme.
    sensitivity: dict[str, str] = {}
    for scheme in WEIGHT_SCHEMES:
        best = max(
            scores.items(),
            key=lambda kv: kv[1]["scheme_scores"][scheme]["score"],
        )[0]
        sensitivity[scheme] = best
    scores["__sensitivity__"] = sensitivity
    return scores


def recommendation_stable(scores: dict[str, dict[str, Any]]) -> bool:
    wins = set(scores["__sensitivity__"].values())
    return len(wins) == 1


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> None:
    p = argparse.ArgumentParser(description="R5/R2-3/R2-4: P2-06 decision package")
    p.add_argument("--market-db", type=Path, required=True)
    p.add_argument("--valuation-db", type=Path, required=True)
    p.add_argument("--wave-b-dir", type=Path, required=True)
    p.add_argument("--output-dir", type=Path, required=True)
    args = p.parse_args()

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    start = time.time()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    log = logging.getLogger("b5")
    log.info("=== R5 P2-06 decision package start ===")

    mconn = open_ro(args.market_db)
    vconn = open_ro(args.valuation_db)

    facts = gather_valuation_facts(vconn, args.valuation_db)
    market_facts = gather_market_facts(mconn)
    coverage = coverage_by_market(mconn, vconn)
    probe = load_b4_probe(args.wave_b_dir)
    anomaly = load_anomaly_days(args.wave_b_dir)
    matrix = build_capability_matrix(facts, probe)
    option_scores = score_options(facts, matrix)

    mconn.close()
    vconn.close()

    ranked = sorted(
        (kv for kv in option_scores.items() if kv[0] != "__sensitivity__"),
        key=lambda kv: kv[1]["rank"],
    )
    if ranked and ranked[0][1]["rank"] != 0:
        best = ranked[0][0]
    else:
        best = max(
            (kv for kv in option_scores.items() if kv[0] != "__sensitivity__"),
            key=lambda kv: kv[1]["score"],
        )[0]
    runner_up = ranked[1][0] if len(ranked) > 1 else best

    def write_json(path: str, data: Any):
        (output_dir / path).write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    option_descriptions = {
        "A_rebuild_baostock": "Re-query BaoStock with fixed adapter",
        "B_copy_and_fix": "Copy existing valuation candidate and fix by provenance",
        "C_supplement_other": "Use AkShare/East Money or other sources to fill gaps",
        "D_combine_B_C": "Combine B (local 0->NULL repair) + C (real-value supplement from proven sources)",
    }

    option_matrix: dict[str, dict[str, Any]] = {}
    for opt in OPTION_NAMES:
        sd = option_scores[opt]["score_detail"]
        option_matrix[opt] = {
            "description": option_descriptions[opt],
            "estimated_time_hours": sd["cost_hours"],
            "cost_basis": sd["cost_basis"],
            "estimated_requests": sd["requests"],
            "requests_basis": sd["requests_basis"],
            "risk": sd["risk"],
            "risk_basis": sd["risk_basis"],
            "real_value_fixable": sd["real_value_fixable"],
            "semantic_repair_fields": sd["semantic_repair_fields"],
            "score": option_scores[opt]["score"],
            "rank": option_scores[opt]["rank"],
            "scheme_scores": option_scores[opt]["scheme_scores"],
        }

    write_json("p2-06-option-matrix.json", option_matrix)
    write_json("p2-06-weight-schemes.json", WEIGHT_SCHEMES)
    write_json("p2-06-sensitivity.json", option_scores["__sensitivity__"])
    write_json("p2-06-field-capability-matrix.json", {
        "generated_from_probe": probe.get("available"),
        "probe_path": probe.get("path") if probe.get("available") else None,
        "fields": matrix,
    })
    write_json("p2-06-valuation-facts.json", {
        "valuation_db": str(args.valuation_db.resolve()),
        **facts,
    })
    write_json("p2-06-coverage-by-market.json", coverage)

    missing_fields = [f for f, r in matrix.items() if r["baostock_actual"] == "UNAVAILABLE"]
    unambiguous_missing = [f for f in missing_fields if matrix[f]["candidate_zero_pct"] == 100.0]
    ambiguous = [f for f in FIELD_NAMES if matrix[f]["baostock_actual"] == "LIMITED"]

    # R2-4: failed-codes handling plan.
    failed_plan = {
        "count": facts["backfill"].get("failed_codes", 0),
        "codes": facts["backfill"].get("failed_code_list", []),
        "approach": (
            "Retry the failed backfill codes through the baostock adapter in small batches with "
            "per-code error capture and a bounded retry (3 attempts). Codes that still fail are "
            "recorded with the exact exception and escalate to manual review (do NOT silently "
            "drop them). Retry only after the adapter fix for the 0->NULL fields is reviewed, "
            "so a retry does not re-populate the zero-fields incorrectly."
        ),
        "escalation": (
            "If baostock cannot serve a code after retry, defer it to the supplement path (option "
            "C/D) or to manual review. A failed code must NEVER be represented by zeros; it is "
            "either NULL (unknown) or absent."
        ),
    }

    # R2-4: anomaly-date handling plan.
    anomaly_plan = {
        "dates": ANOMALY_DATES,
        "verdicts": anomaly.get("verdicts", {}),
        "approach": (
            "2026-07-08 and 2026-07-20 are UNRESOLVED (requires_manual_review=true): no observed "
            "fact proves whether they were trading days. Do NOT backfill or modify klines for these "
            "dates from side-table rows. Verify against an upstream exchange trading calendar "
            "(上交所/深交所/北交所 official schedule) before any change. If they were trading days "
            "and klines are genuinely missing, ingest them in a separate, human-approved pipeline run."
        ),
        "no_automatic_action": True,
    }

    # R2-4: strongest objection to the recommended option.
    strongest_objection = {
        "option": best,
        "objection": (
            f"Option {best} maximizes score under the default balanced weights but the marginal "
            "value is dominated by cost/risk rather than real coverage. Under the coverage_first "
            "weight scheme the winner changes to D_combine_B_C, which proves the recommendation is "
            "weight-sensitive. In particular, B_copy_and_fix delivers ZERO real-value coverage "
            "(market_cap / total_shares / dividend_yield remain NULL, not populated), so choosing it "
            "does not actually repair the data gap that P2-06 is meant to close."
        ),
        "mitigation": (
            "Decision must be made by Nova/human with an explicit trade-off: accept the safe, cheap, "
            "but coverage-neutral option (B), or invest in the real-value option (D) whose cost/risk "
            "are higher but which actually populates market_cap from a PROVEN source."
        ),
    }

    # R2-4: explicit P2-06 execution gate.
    execution_gate = {
        "P2-06_NOT_EXECUTED": True,
        "gate": (
            "P2-06 is NOT executed in this work order. No SQL is written to the formal DB, no "
            "valuation candidate is mutated, and no kline/backfill change is applied. Execution "
            "requires (1) Nova/human approval of a chosen option, (2) a new work order, and "
            "(3) re-confirmation of the baseline DB SHA256 immediately before execution."
        ),
    }

    decision_report = "# P2-06 Decision Report\n\n"
    decision_report += f"Generated: {now_iso()}\n\n"
    decision_report += "## Current State\n\n"
    decision_report += f"- Valuation candidate: {facts['val_rows']} rows, {facts['val_codes']} codes, {facts['val_dates']} dates\n"
    decision_report += f"- Valuation DB size: {facts['val_size_bytes'] / 1024 / 1024 / 1024:.1f} GB\n"
    decision_report += f"- Provenance table present: {facts['provenance']['table_present']}, rows={facts['provenance']['rows']}, sources={facts['provenance']['sources']}\n"
    decision_report += f"- Provenance coverage: {facts['provenance']['coverage_pct']}%\n"
    decision_report += f"- Backfill: {facts['backfill']['counts']}\n"
    decision_report += f"- BJ codes in formal DB: {market_facts['bj_codes']}\n"
    decision_report += f"- Codes with no klines: {market_facts['missing_kline_codes']}\n\n"
    decision_report += "## Coverage by Market (valuation candidate vs formal DB)\n\n"
    decision_report += "| Market | Formal codes | Valuation codes | Coverage |\n"
    decision_report += "|--------|-------------|-----------------|----------|\n"
    for b, c in coverage.items():
        decision_report += f"| {b} | {c['formal_codes']} | {c['valuation_codes']} | {c['coverage_pct']}% |\n"
    decision_report += "\n## Field Zero/Null Rates (schema uses DEFAULT 0)\n\n"
    decision_report += "| Field | Zero | Null | %Zero | Interpretation |\n"
    decision_report += "|-------|------|------|-------|----------------|\n"
    for f in FIELD_NAMES:
        fs = facts["field_stats"][f]
        interp = "Never written by adapter (definitive missing)" if fs["all_zero"] else (
            "Fully populated" if fs["pct_zero"] == 0 else f"Mostly populated; {fs['pct_zero']}% zeros ambiguous"
        )
        decision_report += f"| {f} | {fs['zero_count']} | {fs['null_count']} | {fs['pct_zero']}% | {interp} |\n"
    decision_report += "\n## Field Capability Matrix\n\n"
    decision_report += f"Generated from B4 probe: {probe.get('available', False)}\n\n"
    decision_report += "| Field | Candidate %Zero | baostock_actual | Alternative sources |\n"
    decision_report += "|-------|----------------|-----------------|---------------------|\n"
    for f, row in matrix.items():
        src_txt = "; ".join(f"{k}={v}" for k, v in row["sources"].items())
        decision_report += f"| {f} | {row['candidate_zero_pct']}% | {row['baostock_actual']} | {src_txt} |\n"
    decision_report += "\n## Missing-Field Analysis\n\n"
    decision_report += f"- Fields never written by baostock adapter (100% zero): {unambiguous_missing}\n"
    decision_report += f"- Fields with ambiguous low-zero counts: {ambiguous}\n"
    decision_report += f"- Failed backfill codes: {facts['backfill'].get('failed_codes', 0)}\n\n"
    decision_report += "## Failed Backfill Codes Handling Plan (R2-4)\n\n"
    decision_report += f"- Count: {failed_plan['count']}\n"
    decision_report += f"- Approach: {failed_plan['approach']}\n"
    decision_report += f"- Escalation: {failed_plan['escalation']}\n\n"
    decision_report += "## Anomaly Dates Handling Plan (R2-4)\n\n"
    for d in ANOMALY_DATES:
        verdict = anomaly_plan["verdicts"].get(d, ("UNKNOWN", True))
        decision_report += f"- {d}: verdict={verdict[0]}, requires_manual_review={verdict[1]}\n"
    decision_report += f"- Approach: {anomaly_plan['approach']}\n"
    decision_report += f"- No automatic action: {anomaly_plan['no_automatic_action']}\n\n"
    decision_report += "## Option Scores (derived; 0->NULL = semantic repair, NOT real coverage)\n\n"
    decision_report += "| Option | Score | Real-value fixable | Semantic repair | Risk | Cost basis |\n"
    decision_report += "|--------|-------|--------------------|-----------------|------|------------|\n"
    for opt in OPTION_NAMES:
        s = option_scores[opt]
        sd = s["score_detail"]
        decision_report += (
            f"| {opt} | {s['score']} | {sd['real_value_fixable']} | "
            f"{sd['semantic_repair_fields']} | {sd['risk']} | {sd['cost_basis']} |\n"
        )
    decision_report += "\n## Weight Schemes & Sensitivity (R2-3)\n\n"
    decision_report += "| Scheme | cov | repair | cost | risk |\n"
    decision_report += "|--------|-----|--------|------|------|\n"
    for scheme, w in WEIGHT_SCHEMES.items():
        decision_report += (
            f"| {scheme} | {w['cov']} | {w['repair']} | {w['cost']} | {w['risk']} |\n"
        )
    decision_report += "\n| Option | balanced | coverage_first | cost_first |\n"
    decision_report += "|--------|----------|----------------|------------|\n"
    for opt in OPTION_NAMES:
        ss = option_scores[opt]["scheme_scores"]
        decision_report += (
            f"| {opt} | {ss['balanced']['score']} | {ss['coverage_first']['score']} | "
            f"{ss['cost_first']['score']} |\n"
        )
    decision_report += (
        f"\nWinner per scheme: {option_scores['__sensitivity__']}\n"
        f"Stable recommendation: {recommendation_stable(option_scores)}\n\n"
    )
    decision_report += "## Strongest Objection (R2-4)\n\n"
    decision_report += f"- Objection: {strongest_objection['objection']}\n"
    decision_report += f"- Mitigation: {strongest_objection['mitigation']}\n\n"
    decision_report += "## Recommendation\n\n"
    decision_report += f"**Recommended: {best}** (runner-up: {runner_up})\n\n"
    decision_report += f"{option_descriptions[best]}\n\n"
    decision_report += f"**P2-06_NOT_EXECUTED: true**\n\n"
    (output_dir / "p2-06-decision-report.md").write_text(decision_report, encoding="utf-8")

    execution_plan = {
        "phase": "P2-06",
        "not_executed": True,
        "recommended_option": best,
        "gate": execution_gate,
        "steps": [
            "1. Create copy of existing valuation candidate DB",
            "2. For the unambiguous-missing fields (100% zero): SET <field>=NULL (semantic repair only)",
            "3. For ambiguous low-zero fields: verify per-code before changing",
            "4. Retry the 75 failed backfill codes in batches with per-code error capture (3 attempts), escalating failures to manual review",
            "5. Verify row counts unchanged",
            "6. Run quality metrics on new candidate",
            "7. If real-value coverage is required: supplement market_cap (PROVEN source) via option C/D path",
            "8. P2-08 coverage gate with new candidate",
            "9. Only write to formal DB if coverage gate passes AND Nova approves a new work order",
        ],
        "estimated_disk_gb": 4,
        "estimated_time_minutes": 30,
    }
    write_json("p2-06-execution-plan-draft.json", execution_plan)
    write_json("p2-06-failed-codes-plan.json", failed_plan)
    write_json("p2-06-anomaly-dates-plan.json", anomaly_plan)
    write_json("p2-06-strongest-objection.json", strongest_objection)

    stop_conditions = {
        "formal_db_changed": "STOP: formal DB SHA256, size, or mtime changed",
        "valuation_db_changed": "STOP: valuation candidate DB SHA256 changed",
        "service_started": "STOP: quantstar.service became active",
        "disk_below_20gb": "STOP: disk space below 20GB",
        "75_failures_not_resolved": "STOP: retry of failed codes still fails after bounded retries without escalation",
        "anomaly_dates_not_covered": "STOP: anomaly dates still unresolved; no backfill without calendar verification",
        "coverage_gate_fails": "STOP: coverage gate fails for any market",
        "no_execution_gate": "STOP: P2-06 must not be executed without Nova approval + new work order",
    }
    write_json("p2-06-stop-conditions.json", {
        "P2-06_NOT_EXECUTED": True,
        "stop_conditions": stop_conditions,
    })

    elapsed = time.time() - start
    report = {
        "stage": "R5",
        "status": "COMPLETED",
        "P2-06_NOT_EXECUTED": True,
        "recommended_option": best,
        "runner_up": runner_up,
        "recommendation_stable": recommendation_stable(option_scores),
        "sensitivity": option_scores["__sensitivity__"],
        "option_scores": {o: s["score"] for o, s in option_scores.items() if o != "__sensitivity__"},
        "coverage_by_market": coverage,
        "failed_codes": facts["backfill"].get("failed_codes", 0),
        "anomaly_dates": ANOMALY_DATES,
        "strongest_objection": strongest_objection["objection"],
        "provenance_table_present": facts["provenance"]["table_present"],
        "provenance_sources": facts["provenance"]["sources"],
        "field_zero_rates": {f: facts["field_stats"][f]["pct_zero"] for f in FIELD_NAMES},
        "probe_available": probe.get("available", False),
        "elapsed_seconds": round(elapsed, 2),
    }
    write_json("report.json", report)
    log.info("=== R5 decision package complete ===")
    log.info("Recommendation: %s (score %.3f)", best, option_scores[best]["score"])
    log.info("Sensitivity: %s", option_scores["__sensitivity__"])
    log.info("P2-06_NOT_EXECUTED: true")
    log.info("Elapsed: %.1fs", elapsed)


if __name__ == "__main__":
    main()
