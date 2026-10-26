#!/usr/bin/env python3
"""P2-07B: Audit BJ valuation feasibility — pure local aggregation/reporting.

Reads the probe output (bj-code-mapping.json, probe-results.json,
probe-plan.json, cross-source-consistency.json) and produces:

  - analysis/feasibility-report.json    (all G3 sections + verdict gates)
  - analysis/feasibility-report.md      (human-readable report)
  - analysis/verdict.json               (final verdict)

The verdict is COMPUTED from explicit boolean invariants (§6.1). It is never
written unconditionally. Verdict values are restricted to:

  GO_FOR_ISOLATED_BJ_BUILD
  PARTIAL_FEASIBILITY_ONLY
  NO_GO_SOURCE_OR_MAPPING_INSUFFICIENT

This script performs NO network access and NO database writes. All inputs are
read-only JSON produced by probe_bj_valuation_sources.py.

Exit code: 0 on successful analysis (any verdict is a valid output), 1 on any
processing error.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("audit_bj")

VERDICT_GO = "GO_FOR_ISOLATED_BJ_BUILD"
VERDICT_PARTIAL = "PARTIAL_FEASIBILITY_ONLY"
VERDICT_NO_GO = "NO_GO_SOURCE_OR_MAPPING_INSUFFICIENT"
VERDICTS = {VERDICT_GO, VERDICT_PARTIAL, VERDICT_NO_GO}

STATUS_PROVEN = "PROVEN"
STATUS_LIMITED_SAMPLE = "LIMITED_SAMPLE"
STATUS_FIELD_UNAVAILABLE = "FIELD_UNAVAILABLE"
STATUS_CODE_NOT_FOUND = "CODE_NOT_FOUND"
STATUS_NETWORK_ERROR = "NETWORK_ERROR"
STATUS_ADAPTER_ERROR = "ADAPTER_ERROR"
STATUS_RATE_LIMITED = "RATE_LIMITED"
STATUS_UNPROVEN = "UNPROVEN"
ERROR_STATUSES = {STATUS_NETWORK_ERROR, STATUS_ADAPTER_ERROR, STATUS_RATE_LIMITED}

MAPPING_OFFICIAL_PROVEN = "OFFICIAL_PROVEN"
MAPPING_CANDIDATE_NAME_MATCH = "CANDIDATE_NAME_MATCH"
MAPPING_UNPAIRED_OLD = "UNPAIRED_OLD"
MAPPING_UNPAIRED_NEW = "UNPAIRED_NEW"
MAPPING_CONFLICT = "CONFLICT"
MAPPING_UNRESOLVED = "UNRESOLVED"

HISTORY_FIELDS = {"pe_ttm", "pb_mrq", "ps_ttm", "market_cap", "total_shares"}
TARGET_FIELDS = [
    "pe_ttm", "pb_mrq", "ps_ttm", "market_cap", "total_shares", "dividend_yield",
]

GO_GATES = [
    ("migration_mapping_not_solely_same_name",
     "at least three complete official mappings are migration-usable"),
    ("real_history_pe_pb_from_one_source",
     "at least one source returns a real historical series for PE(TTM) and PB"),
    ("three_securities_three_timepoints",
     "at least 3 distinct BJ securities with >=3 historical time points"),
    ("fields_units_dates_clear",
     "response fields, units and date semantics are explicit"),
    ("no_schema_drift_or_silent_empty",
     "no observed schema drift or silent empty result"),
    ("request_ledger_conserved",
     "request ledger, observations, source totals and runtime counter conserve"),
    ("probe_request_budget_within_limits",
     "probe stays within total/per-source request and code budgets"),
    ("cross_source_three_securities",
     "at least three securities have semantically comparable cross-source values"),
    ("estimated_full_build_bounded",
     "future full-build estimate is reported separately and bounded"),
    ("per_record_provenance_sha",
     "PROVEN records carry response SHA256 and retrieval time"),
    ("no_paid_or_new_credentials",
     "all observed sources are approved free endpoints without credentials"),
    ("failures_checkpoint_resume",
     "checkpoint/resume and non-zero failure propagation were evidenced"),
    ("strongest_objection_not_fatal",
     "mapping, request, consistency and source objections do not negate feasibility"),
]


def _load(path: Path, name: str):
    if not path.is_file():
        raise FileNotFoundError(f"{name} not found: {path}")
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _finite(v: Any) -> float | None:
    if v is None:
        return None
    s = str(v).strip()
    if s in ("", "-", "nan", "NaN", "None"):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return f


def analyze(
    mapping: list[dict], results: list[dict], plan: list[dict],
    consistency: list[dict], request_ledger: list[dict] | None = None,
    execution_evidence: dict | None = None,
) -> dict:
    request_ledger = request_ledger or []
    execution_evidence = execution_evidence or {}
    # ---- 1. mapping distribution ----
    status_counts = Counter(m.get("mapping_status", "") for m in mapping)
    paired = [m for m in mapping if m.get("old_code") and m.get("new_code")]
    def official_complete(m: dict) -> bool:
        return (
            m.get("mapping_status") == MAPPING_OFFICIAL_PROVEN
            and m.get("confidence") == "high"
            and bool(m.get("old_code") and m.get("new_code"))
            and bool(m.get("evidence_url") and m.get("evidence_date"))
            and bool(m.get("evidence_excerpt"))
            and len(str(m.get("evidence_sha256", ""))) == 64
            and bool(m.get("usable_for_migration"))
        )

    official = [m for m in mapping if official_complete(m)]
    candidates = [m for m in mapping if m.get("mapping_status") == MAPPING_CANDIDATE_NAME_MATCH]
    unpaired_old = [m for m in mapping if m.get("mapping_status") == MAPPING_UNPAIRED_OLD]
    unpaired_new = [m for m in mapping if m.get("mapping_status") == MAPPING_UNPAIRED_NEW]
    conflicts = [m for m in mapping if m.get("mapping_status") == MAPPING_CONFLICT]
    unresolved = [m for m in mapping if m.get("mapping_status") == MAPPING_UNRESOLVED]

    all_codes = set()
    for m in mapping:
        if m.get("old_code"):
            all_codes.add(m["old_code"])
        if m.get("new_code"):
            all_codes.add(m["new_code"])

    mapping_counts = {
        "total_mapping_entries": len(mapping),
        "total_unique_codes": len(all_codes),
        "paired": len(paired),
        "official_proven": len(official),
        "candidate_name_match": len(candidates),
        "unpaired_old": len(unpaired_old),
        "unpaired_new": len(unpaired_new),
        "conflict": len(conflicts),
        "unresolved": len(unresolved),
    }
    total_mapping = max(len(mapping), 1)
    mapping_counts["official_mapping_coverage"] = round(len(official) / total_mapping, 6)
    mapping_counts["candidate_mapping_coverage"] = round(
        (len(candidates) + len(official)) / total_mapping, 6,
    )

    probe_usable = [m for m in mapping if m.get("usable_for_probe")]
    migration_usable = [m for m in mapping if m.get("usable_for_migration")]
    mapping_counts["probe_usable"] = len(probe_usable)
    mapping_counts["migration_usable"] = len(migration_usable)

    # ---- 2. probe request stats (request ledger is authoritative) ----
    plan_codes = {r.get("code") for r in plan if r.get("code")}
    probe_codes = {r.get("code_requested") for r in results if r.get("code_requested")}
    ledger_rows = [r for r in request_ledger if r.get("counted_as_real_request")]
    request_ids = [str(r.get("request_id", "")) for r in ledger_rows]
    request_id_set = {rid for rid in request_ids if rid}
    request_counts_by_source = Counter(
        str(r.get("source", "")) for r in ledger_rows if r.get("source")
    )
    observation_request_ids = {
        str(r.get("request_id", "")) for r in results if r.get("source")
    }
    missing_request_refs = sorted(observation_request_ids - request_id_set)
    duplicate_request_ids = len(request_ids) - len(request_id_set)
    runtime_request_count = execution_evidence.get("runtime_real_request_count")
    runtime_matches = (
        isinstance(runtime_request_count, int)
        and runtime_request_count == len(ledger_rows)
    )
    per_source_runtime = execution_evidence.get("runtime_requests_by_source", {})
    per_source_matches = bool(per_source_runtime) and dict(sorted(request_counts_by_source.items())) == dict(sorted(per_source_runtime.items()))
    ledger_conserved = (
        bool(ledger_rows)
        and duplicate_request_ids == 0
        and not missing_request_refs
        and runtime_matches
        and per_source_matches
    )
    probe_stats = {
        "plan_codes": len(plan_codes),
        "probe_codes": len(probe_codes),
        "real_request_count": len(ledger_rows),
        "request_counts_by_source": dict(sorted(request_counts_by_source.items())),
        "runtime_real_request_count": runtime_request_count,
        "runtime_requests_by_source": per_source_runtime,
        "duplicate_request_ids": duplicate_request_ids,
        "missing_observation_request_refs": missing_request_refs,
        "request_ledger_conserved": ledger_conserved,
    }

    # ---- 3. per-field classification (dedupe by code+field+date) ----
    dedup_key = set()
    deduped = []
    for r in results:
        code = r.get("code_requested", "")
        field = r.get("field", "")
        asof = r.get("as_of_date", "")
        src = r.get("source", "")
        key = (src, code, field, asof)
        if key in dedup_key:
            continue
        dedup_key.add(key)
        deduped.append(r)

    field_status: dict[str, Counter] = {}
    for field in TARGET_FIELDS:
        field_status[field] = Counter(r.get("status", STATUS_UNPROVEN) for r in deduped if r.get("field") == field)

    field_counts = {
        field: {
            "PROVEN": field_status[field].get(STATUS_PROVEN, 0),
            "LIMITED_SAMPLE": field_status[field].get(STATUS_LIMITED_SAMPLE, 0),
            "FIELD_UNAVAILABLE": field_status[field].get(STATUS_FIELD_UNAVAILABLE, 0),
            "CODE_NOT_FOUND": field_status[field].get(STATUS_CODE_NOT_FOUND, 0),
            "NETWORK_ERROR": field_status[field].get(STATUS_NETWORK_ERROR, 0),
            "ADAPTER_ERROR": field_status[field].get(STATUS_ADAPTER_ERROR, 0),
            "RATE_LIMITED": field_status[field].get(STATUS_RATE_LIMITED, 0),
            "UNPROVEN": field_status[field].get(STATUS_UNPROVEN, 0),
        }
        for field in TARGET_FIELDS
    }

    # ---- 4. history availability ----
    hist_results = [r for r in deduped if r.get("is_historical_series") and r.get("status") == STATUS_PROVEN]
    hist_by_field: dict[str, set] = {}
    for r in hist_results:
        hist_by_field.setdefault(r.get("field", ""), set()).add(r.get("code_requested", ""))
    history_fields_available = {
        f: sorted(hist_by_field.get(f, [])) for f in HISTORY_FIELDS
    }
    history_fields_code_count = {
        f: len(hist_by_field.get(f, [])) for f in HISTORY_FIELDS
    }

    # earliest/latest dates per field
    date_ranges: dict[str, dict] = {}
    for f in HISTORY_FIELDS:
        dates = [r.get("as_of_date") for r in hist_results if r.get("field") == f and r.get("as_of_date")]
        dates = [d for d in dates if d]
        if dates:
            date_ranges[f] = {"earliest": min(dates), "latest": max(dates), "samples": len(dates)}
        else:
            date_ranges[f] = {"earliest": "", "latest": "", "samples": 0}

    # per-year continuity sampling: distinct years covered per (field, code)
    year_cov: Counter = Counter()
    for r in hist_results:
        asof = r.get("as_of_date", "")
        if len(asof) >= 4:
            year_cov[(r.get("field", ""), asof[:4])] += 1
    yearly_continuity = {}
    for f in sorted(HISTORY_FIELDS):
        years = sorted({y for (fl, y) in year_cov if fl == f})
        yearly_continuity[f] = years

    # ---- 5. securities with >=3 time points ----
    hist_per_code: dict[str, set] = {}
    for r in hist_results:
        hist_per_code.setdefault(r.get("code_requested", ""), set()).add(r.get("as_of_date", ""))
    securities_3_timepoints = [c for c, ds in hist_per_code.items() if len(ds) >= 3]

    # ---- 6. old/new code query boundary ----
    old_results = [r for r in deduped if r.get("code_type") == "old"]
    new_results = [r for r in deduped if r.get("code_type") == "new"]
    old_err = Counter(r.get("status") for r in old_results if r.get("status") in ERROR_STATUSES)
    new_err = Counter(r.get("status") for r in new_results if r.get("status") in ERROR_STATUSES)
    old_notfound = sum(1 for r in old_results if r.get("status") == STATUS_CODE_NOT_FOUND)
    new_notfound = sum(1 for r in new_results if r.get("status") == STATUS_CODE_NOT_FOUND)
    query_boundary = {
        "old_code_error_statuses": dict(old_err),
        "new_code_error_statuses": dict(new_err),
        "old_code_not_found": old_notfound,
        "new_code_not_found": new_notfound,
        "old_code_notes": "old 83xxxx/43xxxx codes are probed as-requested; "
                          "Eastmoney stock_value_em returns result=null -> ADAPTER_ERROR (not verified absence)",
    }

    # ---- 7. schema drift / silent empty / fusing ----
    schema_drift = sum(1 for r in deduped if r.get("error_class") == "schema_drift")
    silent_empty = sum(1 for r in deduped if r.get("status") == STATUS_FIELD_UNAVAILABLE
                       and r.get("error") in ("empty response", "empty dataframe"))
    rate_limited = sum(1 for r in deduped if r.get("status") == STATUS_RATE_LIMITED)

    # ---- 8. cross-source consistency ----
    comparable_rows = [
        c for c in consistency
        if c.get("comparison_status") in ("SAMPLE_ALIGNED", "SAMPLE_DIFFERENT")
    ]
    consistent_rows = [c for c in comparable_rows if c.get("sample_consistent_le_2pct")]
    inconsistent_rows = [c for c in comparable_rows if not c.get("sample_consistent_le_2pct")]
    incomparable_rows = [c for c in consistency if c.get("comparison_status") == "INCOMPARABLE"]
    comparable_codes = sorted({str(c.get("code")) for c in comparable_rows if c.get("code")})
    consistency_stats = {
        "comparisons": len(consistency),
        "comparable": len(comparable_rows),
        "comparable_securities": comparable_codes,
        "comparable_security_count": len(comparable_codes),
        "sample_consistent_le_2pct": len(consistent_rows),
        "inconsistent": len(inconsistent_rows),
        "incomparable": len(incomparable_rows),
        "inconsistent_details": inconsistent_rows,
        "incomparable_details": incomparable_rows,
    }

    # ---- 9. full-build request estimate ----
    universe_new_codes = len([m for m in mapping if m.get("new_code")])
    # Per code: stock_value_em (1) + comparison (1) + baidu 2 indicators (2) + spot batch (1 total)
    per_code_requests = 1 + 1 + 2  # value_em + comparison + baidu(pe,pb)
    batch_requests = 1
    est_full_requests = universe_new_codes * per_code_requests + batch_requests
    est_seconds_per_request = 1.5
    est_wall_minutes = round(est_full_requests * est_seconds_per_request / 60.0, 1)
    build_estimate = {
        "universe_new_codes": universe_new_codes,
        "per_code_requests": per_code_requests,
        "batch_requests": batch_requests,
        "estimated_total_requests": est_full_requests,
        "estimated_wall_minutes": est_wall_minutes,
        "recovery_strategy": "per-code checkpoint; resume from last persisted code; "
                             "single-source fusing on 3 consecutive failures; "
                             "batch source fetched once per run",
    }

    # ---- 10. verdict gates (all derived from supplied evidence) ----
    gates: dict[str, dict] = {}

    def gate(passed: bool, detail: str, evidence: str) -> dict:
        return {"passed": bool(passed), "detail": detail, "evidence": evidence}

    official_pairs = sorted((m.get("old_code"), m.get("new_code")) for m in official)
    gates["migration_mapping_not_solely_same_name"] = gate(
        len(official) >= 3,
        f"complete migration-usable official mappings={len(official)}",
        f"bj-code-mapping.json official pairs={official_pairs[:10]}",
    )
    history_codes_by_source_field: dict[str, dict[str, set[str]]] = {}
    for row in hist_results:
        source = str(row.get("source", ""))
        field = str(row.get("field", ""))
        code = str(row.get("code_requested", ""))
        if source and field in ("pe_ttm", "pb_mrq") and code:
            history_codes_by_source_field.setdefault(source, {}).setdefault(field, set()).add(code)
    same_source_pe_pb = {
        source: sorted(
            fields.get("pe_ttm", set()) & fields.get("pb_mrq", set()),
        )
        for source, fields in history_codes_by_source_field.items()
    }
    qualifying_history_sources = {
        source: codes for source, codes in same_source_pe_pb.items() if len(codes) >= 3
    }
    gates["real_history_pe_pb_from_one_source"] = gate(
        bool(qualifying_history_sources),
        f"same-source PE/PB history coverage={qualifying_history_sources}",
        "probe-results.json historical PROVEN rows grouped by source/field/code",
    )
    gates["three_securities_three_timepoints"] = gate(
        len(securities_3_timepoints) >= 3,
        f"securities with >=3 historical time points={len(securities_3_timepoints)}",
        "probe-results.json code/date history",
    )
    proven_rows = [r for r in deduped if r.get("status") == STATUS_PROVEN]
    fields_clear = all(
        r.get("unit") and (r.get("as_of_date") or not r.get("is_historical_series"))
        for r in proven_rows
    )
    gates["fields_units_dates_clear"] = gate(
        fields_clear, "all PROVEN observations have unit and valid date semantics",
        "probe-results.json unit/as_of_date/is_historical_series",
    )
    gates["no_schema_drift_or_silent_empty"] = gate(
        schema_drift == 0 and silent_empty == 0,
        f"schema_drift={schema_drift}, silent_empty={silent_empty}, rate_limited={rate_limited}",
        "probe-results.json error_class/status/error",
    )
    gates["request_ledger_conserved"] = gate(
        ledger_conserved,
        f"ledger={len(ledger_rows)}, runtime={runtime_request_count}, "
        f"duplicate_ids={duplicate_request_ids}, missing_refs={len(missing_request_refs)}, "
        f"per_source_match={per_source_matches}",
        "request-ledger.json + execution-evidence.json + probe-results.json",
    )
    within_budget = (
        len(plan_codes) <= 12
        and len(ledger_rows) <= 80
        and all(v <= 30 for v in request_counts_by_source.values())
        and bool(execution_evidence.get("wall_clock_within_20_minutes"))
    )
    gates["probe_request_budget_within_limits"] = gate(
        within_budget,
        f"codes={len(plan_codes)}/12, requests={len(ledger_rows)}/80, "
        f"max_per_source={max(request_counts_by_source.values(), default=0)}/30, "
        f"wall_clock_ok={execution_evidence.get('wall_clock_within_20_minutes')}",
        "probe-plan.json + request-ledger.json + execution-evidence.json",
    )
    gates["cross_source_three_securities"] = gate(
        len(comparable_codes) >= 3,
        f"comparable securities={len(comparable_codes)}; comparisons={len(comparable_rows)}",
        "cross-source-consistency.json",
    )
    gates["estimated_full_build_bounded"] = gate(
        est_full_requests <= 2000,
        f"estimated full-build requests={est_full_requests}",
        "computed from mapped BJ universe and source request model",
    )
    provenance_ok = bool(proven_rows) and all(
        r.get("response_sha256") and r.get("retrieved_at") for r in proven_rows
    )
    gates["per_record_provenance_sha"] = gate(
        provenance_ok,
        "all PROVEN records carry response_sha256 and retrieved_at",
        "probe-results.json",
    )
    approved_sources = set(execution_evidence.get("approved_free_sources", []))
    used_sources = {str(r.get("source")) for r in results if r.get("source")}
    credentials_ok = bool(approved_sources) and used_sources <= approved_sources and not execution_evidence.get("new_credentials_used", True)
    gates["no_paid_or_new_credentials"] = gate(
        credentials_ok,
        f"used_sources={sorted(used_sources)}, approved_free={sorted(approved_sources)}, "
        f"new_credentials_used={execution_evidence.get('new_credentials_used')}",
        "execution-evidence.json source policy",
    )
    checkpoint_ok = (
        execution_evidence.get("checkpoint_resume_test_passed") is True
        and execution_evidence.get("nonzero_error_propagation_test_passed") is True
    )
    gates["failures_checkpoint_resume"] = gate(
        checkpoint_ok,
        f"checkpoint_resume={execution_evidence.get('checkpoint_resume_test_passed')}, "
        f"nonzero_propagation={execution_evidence.get('nonzero_error_propagation_test_passed')}",
        "execution-evidence.json linked test evidence",
    )
    fatal_objections = []
    if len(official) < 3:
        fatal_objections.append("official_mapping")
    if not ledger_conserved:
        fatal_objections.append("request_ledger")
    if len(comparable_codes) < 3:
        fatal_objections.append("cross_source_coverage")
    if schema_drift or silent_empty:
        fatal_objections.append("source_schema_or_silent_empty")
    if any(c.get("relative_difference", 0) > 0.02 for c in inconsistent_rows):
        fatal_objections.append("cross_source_difference_gt_2pct")
    gates["strongest_objection_not_fatal"] = gate(
        not fatal_objections,
        f"fatal objections={fatal_objections}",
        "mapping/request/anomaly/cross-source aggregate",
    )

    all_gates_pass = all(g["passed"] for g in gates.values())

    # Core feasibility (needed for at least PARTIAL).
    core_history_ok = gates["real_history_pe_pb_from_one_source"]["passed"]
    core_securities_ok = gates["three_securities_three_timepoints"]["passed"]
    core_fields_ok = gates["fields_units_dates_clear"]["passed"]

    if all_gates_pass:
        verdict = VERDICT_GO
        verdict_reason = "all explicit P2-07B-R1 gates pass"
    elif core_history_ok and core_securities_ok and core_fields_ok:
        verdict = VERDICT_PARTIAL
        failed = [k for k, g in gates.items() if not g["passed"]]
        verdict_reason = (
            "core history/securities/fields feasibility holds, but not all gates pass; "
            f"failed gates: {failed}"
        )
    else:
        verdict = VERDICT_NO_GO
        verdict_reason = (
            "core historical series / multi-security / field-semantics feasibility "
            "not established"
        )

    report = {
        "generated_at": _now(),
        "inputs": {
            "mapping_entries": len(mapping),
            "probe_results": len(results),
            "probe_plan_codes": len(plan),
            "request_ledger_entries": len(request_ledger),
            "cross_source_comparisons": len(consistency),
        },
        "mapping_distribution": mapping_counts,
        "probe_stats": probe_stats,
        "field_status": field_counts,
        "history": {
            "fields_by_code": history_fields_available,
            "fields_code_count": history_fields_code_count,
            "date_ranges": date_ranges,
            "yearly_continuity": yearly_continuity,
            "securities_3_timepoints": securities_3_timepoints,
        },
        "query_boundary": query_boundary,
        "anomalies": {
            "schema_drift_count": schema_drift,
            "silent_empty_count": silent_empty,
            "rate_limited_count": rate_limited,
        },
        "cross_source_consistency": consistency_stats,
        "full_build_estimate": build_estimate,
        "verdict_gates": gates,
        "all_gates_pass": all_gates_pass,
        "verdict": verdict,
        "verdict_reason": verdict_reason,
    }
    return report


def _write_report(report: dict, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "feasibility-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8",
    )
    (output_dir / "verdict.json").write_text(
        json.dumps({
            "verdict": report["verdict"],
            "reason": report["verdict_reason"],
            "all_gates_pass": report["all_gates_pass"],
            "gates": report["verdict_gates"],
        }, ensure_ascii=False, indent=2), encoding="utf-8",
    )

    L = []
    L.append("# QuantStar P2-07B — BJ Valuation Feasibility Report")
    L.append("")
    L.append(f"Generated: {report['generated_at']}")
    L.append("")
    L.append(f"**Verdict: {report['verdict']}**")
    L.append("")
    L.append(f"Reason: {report['verdict_reason']}")
    L.append("")
    L.append("## 1. Inputs")
    L.append("")
    L.append(f"- mapping entries: {report['inputs']['mapping_entries']}")
    L.append(f"- probe results: {report['inputs']['probe_results']}")
    L.append(f"- probe plan codes: {report['inputs']['probe_plan_codes']}")
    L.append(f"- cross-source comparisons: {report['inputs']['cross_source_comparisons']}")
    L.append("")
    L.append("## 2. Mapping Distribution")
    L.append("")
    L.append("| Metric | Count |")
    L.append("|--------|-------|")
    md = report["mapping_distribution"]
    for k, v in md.items():
        L.append(f"| {k} | {v} |")
    L.append("")
    L.append("## 3. Probe Stats")
    L.append("")
    L.append("| Metric | Value |")
    L.append("|--------|-------|")
    for k, v in report["probe_stats"].items():
        L.append(f"| {k} | {v} |")
    L.append("")
    L.append("## 4. Per-Field Status (deduplicated by code+date+source)")
    L.append("")
    L.append("| Field | PROVEN | LIMITED | UNAVAIL | NOT_FOUND | NET_ERR | ADAPTER | RATE | UNPROVEN |")
    L.append("|-------|--------|---------|---------|-----------|---------|---------|------|----------|")
    for f in TARGET_FIELDS:
        c = report["field_status"][f]
        L.append(
            f"| {f} | {c['PROVEN']} | {c['LIMITED_SAMPLE']} | {c['FIELD_UNAVAILABLE']} "
            f"| {c['CODE_NOT_FOUND']} | {c['NETWORK_ERROR']} | {c['ADAPTER_ERROR']} "
            f"| {c['RATE_LIMITED']} | {c['UNPROVEN']} |"
        )
    L.append("")
    L.append("## 5. History Availability")
    L.append("")
    L.append("| Field | Codes | Earliest | Latest | Samples |")
    L.append("|-------|-------|----------|--------|---------|")
    for f in HISTORY_FIELDS:
        h = report["history"]
        dr = h["date_ranges"][f]
        L.append(f"| {f} | {h['fields_code_count'][f]} | {dr['earliest']} | {dr['latest']} | {dr['samples']} |")
    L.append("")
    L.append("Securities with >=3 historical time points: "
             + ", ".join(report["history"]["securities_3_timepoints"]) or "none")
    L.append("")
    L.append("## 6. Query Boundary (old vs new code)")
    L.append("")
    L.append(f"- old code error statuses: {report['query_boundary']['old_code_error_statuses']}")
    L.append(f"- new code error statuses: {report['query_boundary']['new_code_error_statuses']}")
    L.append(f"- old code not-found: {report['query_boundary']['old_code_not_found']}")
    L.append(f"- new code not-found: {report['query_boundary']['new_code_not_found']}")
    L.append("")
    L.append("## 7. Anomalies")
    L.append("")
    L.append(f"- schema drift: {report['anomalies']['schema_drift_count']}")
    L.append(f"- silent empty: {report['anomalies']['silent_empty_count']}")
    L.append(f"- rate limited: {report['anomalies']['rate_limited_count']}")
    L.append("")
    L.append("## 8. Cross-Source Consistency")
    L.append("")
    L.append(f"- comparisons: {report['cross_source_consistency']['comparisons']}")
    L.append(f"- sample-consistent (<=2%): {report['cross_source_consistency']['sample_consistent_le_2pct']}")
    L.append(f"- inconsistent: {report['cross_source_consistency']['inconsistent']}")
    L.append("")
    L.append("## 9. Full-Build Estimate")
    L.append("")
    L.append("| Metric | Value |")
    L.append("|--------|-------|")
    for k, v in report["full_build_estimate"].items():
        L.append(f"| {k} | {v} |")
    L.append("")
    L.append("## 10. Verdict Gates (all explicit booleans)")
    L.append("")
    L.append("| Gate | Passed | Detail |")
    L.append("|------|--------|--------|")
    for k, g in report["verdict_gates"].items():
        L.append(f"| {k} | {'YES' if g['passed'] else 'no'} | {g['detail']} |")
    L.append("")
    L.append(f"All gates pass: {report['all_gates_pass']}")
    L.append("")
    L.append("## Verdict")
    L.append("")
    L.append(f"**{report['verdict']}**")
    L.append("")

    (output_dir / "feasibility-report.md").write_text("\n".join(L), encoding="utf-8")
    log.info("  wrote feasibility-report.json/.md and verdict.json")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Audit BJ valuation feasibility from probe outputs (local only)",
    )
    p.add_argument("--input-dir", required=True,
                   help="Directory containing probe output (bj-code-mapping.json, "
                        "probe-results.json, probe-plan.json, cross-source-consistency.json)")
    p.add_argument("--output-dir", required=True,
                   help="Directory for feasibility report")
    args = p.parse_args(argv)

    input_dir = Path(args.input_dir).resolve()
    output_dir = Path(args.output_dir).resolve()
    if not input_dir.is_dir():
        log.error("input dir not found: %s", input_dir)
        return 1

    try:
        mapping = _load(input_dir / "bj-code-mapping.json", "bj-code-mapping.json")
        results = _load(input_dir / "probe-results.json", "probe-results.json")
        plan = _load(input_dir / "probe-plan.json", "probe-plan.json")
        consistency = _load(
            input_dir / "cross-source-consistency.json",
            "cross-source-consistency.json",
        )
        request_ledger = _load(
            input_dir / "request-ledger.json", "request-ledger.json",
        )
        execution_evidence = _load(
            input_dir / "execution-evidence.json", "execution-evidence.json",
        )
    except FileNotFoundError as e:
        log.error("missing required input: %s", e)
        return 1

    try:
        report = analyze(
            mapping, results, plan, consistency,
            request_ledger=request_ledger,
            execution_evidence=execution_evidence,
        )
    except Exception as e:  # noqa: BLE001
        log.error("analysis failed: %s: %s", type(e).__name__, e)
        return 1
    _write_report(report, output_dir)
    log.info("verdict: %s", report["verdict"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
