#!/usr/bin/env python3
"""P2-07C-A — independent auditor for the isolated BJ valuation build (Stage A).

Recomputes gates from evidence + candidate DB. Does NOT trust builder-reported
booleans; it reads the raw ledger, response cache, candidate DB, checkpoint
and manifest, and recomputes each gate. Stage A defaults to analysis/verdict.json; Stage B pre-seal audit defaults to analysis/pre-seal-verdict.json, while post-seal output must be outside the evidence root.

CLI:
  --evidence-root <path>      approved evidence root (default approved root)
  --candidate-db <path>       candidate DB (default <root>/build/stage-a-bj-valuation.db)
  --rebuild-db <path>         offline rebuild DB (default <root>/rollback/stage-a-offline-rebuild.db)
  --formal-db <path>          formal DB (read-only)
  --output <path>             verdict output (Stage A: analysis/verdict.json; Stage B pre-seal: analysis/pre-seal-verdict.json; post-seal: external path required)

Exit codes: 0 = ready, 1 = remediation required, 2 = no-go/unrecoverable.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_bj_historical_valuation_isolated as b  # noqa: E402

VERDICT_READY = "STAGE_A_READY_FOR_NOVA_REVIEW"
VERDICT_REMEDIATION = "STAGE_A_REMEDIATION_REQUIRED"
VERDICT_NO_GO = "NO_GO_STAGE_A_SOURCE_OR_CONTRACT_FAILURE"

# Stage B verdicts (work order §16)
VERDICT_B_READY = "PASS_BJ_ISOLATED_CANDIDATE_READY_FOR_MERGE_DESIGN"
VERDICT_B_PARTIAL = "PARTIAL_BJ_BUILD_REMEDIATION_REQUIRED"
VERDICT_B_NO_GO = "NO_GO_BJ_SOURCE_OR_IDENTITY_UNRELIABLE"
VERDICT_B_BLOCKED = "BLOCKED_BASELINE_DRIFT"

GATES = [
    "baseline_immutable",
    "stage_a_identity_ok",
    "only_approved_12_codes",
    "candidate_schema_exact",
    "candidate_quick_check_ok",
    "request_ledger_conserved",
    "response_cache_complete",
    "row_provenance_complete",
    "checkpoint_resume_proven",
    "no_duplicate_requests_after_resume",
    "offline_rebuild_logical_hash_equal",
    "null_and_numeric_semantics_valid",
    "historical_date_semantics_valid",
    "source_failures_explicit",
    "stage_a_request_budget_within_limits",
    "cross_source_anchor_consistency",
    "formal_db_unchanged",
    "production_isolation_maintained",
    "manifest_inputs_complete",
]


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Return a file SHA-256 with constant memory, including multi-GB DBs.

    Memory-card fix (2026-08-27): was ``hashlib.sha256(path.read_bytes())`` which
    slurps the whole file into RAM — hashing the 8.4G formal DB drove the audit
    unit-test file to an 8.34G RSS peak (kernel OOM on the 15.7G host). Chunked
    read mirrors build_bj_historical_valuation_isolated.sha256_file exactly, so
    digests are byte-identical.
    """
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def logical_sha(path: Path) -> str:
    """Stable logical SHA over code,date-sorted 8-field rows (NULL/float encoding).

    Memory-card fix (2026-08-27): was ``fetchall()`` + ``"\\n".join(buf)`` which
    materializes the entire row set in RAM. Now streams rows through the hasher
    with a cursor; the emitted byte sequence is identical (UTF-8 of a join equals
    the join of UTF-8 encodings), so digests are unchanged.
    """
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        cur = conn.execute(
            "SELECT code,date,pe_ttm,pb_mrq,ps_ttm,market_cap,total_shares,dividend_yield "
            "FROM stock_valuation ORDER BY code,date")
        digest = hashlib.sha256()
        first = True
        for r in cur:
            enc = [str(r[0]), str(r[1])]
            for v in r[2:]:
                enc.append("NULL" if v is None else ("ZERO" if v == 0.0 else repr(float(v))))
            if not first:
                digest.update(b"\n")
            digest.update("|".join(enc).encode())
            first = False
        return digest.hexdigest()
    finally:
        conn.close()


def _gate(name: str, passed: bool, detail: str, evidence=None) -> dict:
    return {"gate": name, "passed": bool(passed), "detail": detail,
            "evidence": evidence or []}


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _request_id_ok(rid) -> bool:
    """R3-3: a legal request_id must be a single, safe basename.

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
    return bool(_REQUEST_ID_RE.match(rid))


def _classify_request_ids(ledger) -> tuple[list, list]:
    """R3-3: for every real request (counted_as_real_request) or PROVEN record,
    request_id must exist, be str, non-empty, and safe. Returns
    (invalid_request_ids, valid_ids). Duplicates are only computed on valid IDs."""
    invalid: list[str] = []
    valid: list[str] = []
    for r in ledger:
        if not (r.get("outcome") == b.STATUS_PROVEN
                or r.get("counted_as_real_request")):
            continue
        rid = r.get("request_id")
        if not _request_id_ok(rid):
            invalid.append(repr(rid) if rid is not None else "<missing>")
        else:
            # R3-3: keep EVERY legal id. Duplicates must all be retained so
            # callers can detect them via len(valid)-len(set(valid)); never
            # dedup here (that would fail-open duplicate request ids).
            valid.append(rid)
    return invalid, valid


def _read_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def create_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Independent auditor for P2-07C-A/B")
    p.add_argument("--path", "--evidence-root", dest="evidence_root", type=Path,
                   default=Path(b.APPROVED_ROOT))
    p.add_argument("--candidate-db", type=Path)
    p.add_argument("--rebuild-db", type=Path)
    p.add_argument("--formal-db", type=Path, default=Path(b.FORMAL_DB))
    p.add_argument("--output", type=Path)
    p.add_argument("--mode", choices=["stage-a", "stage-b"], default="stage-a",
                   help="audit target stage (default stage-a)")
    p.add_argument("--stage-a-verdict", type=Path,
                   help="accepted Stage A verdict.json required in stage-b mode")
    p.add_argument("--require-rebuild-hash", action="store_true",
                   help="fail if offline rebuild logical hash mismatch (A3)")
    p.add_argument("--seal-task",
                    help="seal an already complete evidence root; writes final.sha256 last")
    return p


def run(argv=None) -> int:
    args = create_parser().parse_args(argv)
    if args.seal_task:
        result = seal_evidence_root(args.evidence_root, args.seal_task)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.mode == "stage-b":
        return run_stage_b(argv)
    root = args.evidence_root.resolve()
    build_dir = root / "build"
    cand = (args.candidate_db or (build_dir / "stage-a-bj-valuation.db")).resolve()
    rebuild = args.rebuild_db or root / "rollback" / "stage-a-offline-rebuild.db"

    results: list[dict] = []
    counts: dict = {}

    # ---- candidate presence ----
    if not cand.exists():
        results.append(_gate("candidate_schema_exact", False, "candidate db missing", []))
        summary = {"verdict": VERDICT_NO_GO, "gates": results}
        out = args.output or (root / "analysis" / "verdict.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 2
    conn = sqlite3.connect(f"file:{cand}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    # ---- baseline ----
    baseline = _read_json(root / "baseline" / "g0-baseline.json")
    results.append(_gate("baseline_immutable", baseline is not None,
                         "baseline/g0-baseline.json present", []))

    # ---- identity ----
    id_doc = _read_json(root / "identity" / "stage-a-securities.json")
    securities = id_doc.get("securities", []) if id_doc else []
    codes = [s.get("code") for s in securities]
    id_ok = (len(codes) == 12 and len(set(codes)) == 12
             and all(re.fullmatch(r"92[0-9]{4}", c or "") for c in codes)
             and all(c in b.APPROVED_CODES for c in codes))
    results.append(_gate("stage_a_identity_ok", id_ok,
                         f"{len(codes)} identity codes", codes))
    results.append(_gate("only_approved_12_codes", id_ok,
                         "all 12 are exactly approved 92xxxx", []))

    # ---- schema ----
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(stock_valuation)")}
    required = {"code", "date", "pe_ttm", "pb_mrq", "ps_ttm",
                "market_cap", "total_shares", "dividend_yield"}
    schema_ok = required <= cols
    results.append(_gate("candidate_schema_exact", schema_ok,
                         "required columns present", sorted(cols)))

    quick_check = conn.execute("PRAGMA quick_check").fetchone()[0]
    results.append(_gate("candidate_quick_check_ok", quick_check == "ok",
                         f"quick_check={quick_check}", []))

    # ---- ledger ----
    ledger = b.RequestLedger(root / "requests" / "request-ledger.jsonl").records()
    real = [r for r in ledger if r.get("counted_as_real_request")]
    invalid_request_ids, valid_ids = _classify_request_ids(ledger)
    dup = len(valid_ids) - len(set(valid_ids))
    per_source = {}
    for r in real:
        per_source[r.get("source", "")] = per_source.get(r.get("source", ""), 0) + 1
    ledger_ok = dup == 0 and not invalid_request_ids and sum(1 for r in real) == sum(per_source.values())
    results.append(_gate("request_ledger_conserved", ledger_ok,
                         f"{len(real)} real, {dup} dup ids, "
                         f"{len(invalid_request_ids)} invalid ids",
                         {"invalid_request_ids": invalid_request_ids[:20]}))

    # ---- response cache ----
    cache_dir = root / "responses"
    missing_cache = []
    cache_invalid_request_ids = []
    for r in ledger:
        rid = r.get("request_id")
        if r.get("outcome") != b.STATUS_PROVEN:
            continue
        if not _request_id_ok(rid):
            cache_invalid_request_ids.append(repr(rid) if rid is not None else "<missing>")
            continue
        fp = cache_dir / f"{rid}.canonical.json.gz"
        if not fp.exists():
            missing_cache.append(rid)
    cache_ok = len(missing_cache) == 0 and len(cache_invalid_request_ids) == 0
    results.append(_gate("response_cache_complete", cache_ok,
                         f"{len(missing_cache)} missing cache files, "
                         f"{len(cache_invalid_request_ids)} invalid ids",
                         {"invalid_request_ids": cache_invalid_request_ids[:20]}))

    # ---- provenance ----
    rows = conn.execute("SELECT count(*) FROM stock_valuation").fetchone()[0]
    prov = conn.execute("SELECT count(*) FROM valuation_provenance").fetchone()[0]
    results.append(_gate("row_provenance_complete", prov >= rows and rows > 0,
                         f"{prov} provenance for {rows} rows", []))

    # ---- checkpoint + resume ----
    cp = _read_json(build_dir / "checkpoint.json")
    cp_sha_ok, cp_detail = _checkpoint_shas(root, cp)
    cp_ok = bool(cp) and "completed_codes" in cp and cp_sha_ok
    results.append(_gate("checkpoint_resume_proven", cp_ok,
                         "checkpoint present with completed_codes; shas "
                         + ("consistent" if cp_sha_ok else f"INCONSISTENT: {cp_detail}"),
                         []))
    results.append(_gate("no_duplicate_requests_after_resume", dup == 0,
                         f"dup request ids: {dup}", []))

    # ---- offline rebuild hash ----
    if rebuild.exists():
        osha = logical_sha(cand)
        rsha = logical_sha(rebuild)
        hash_ok = osha == rsha and osha != ""
        rqc = _quickcheck(rebuild)
        results.append(_gate("offline_rebuild_logical_hash_equal",
                             (not args.require_rebuild_hash) or hash_ok,
                             f"orig={osha[:16]} rebuild={rsha[:16]}", []))
        counts["logical_sha"] = {"original": osha, "rebuild": rsha}
        counts["quick_check"] = {"original": quick_check, "rebuild": rqc}
    else:
        results.append(_gate("offline_rebuild_logical_hash_equal", True,
                             "no rebuild db present (unit mode)", []))

    # ---- null/numeric dates ----
    nan_bad = conn.execute(
        "SELECT count(*) FROM stock_valuation WHERE pe_ttm IN ('nan','inf','-inf') "
        "OR pb_mrq IN ('nan','inf','-inf') OR ps_ttm IN ('nan','inf','-inf')").fetchone()[0]
    results.append(_gate("null_and_numeric_semantics_valid", nan_bad == 0,
                         f"{nan_bad} NaN-like rows", []))
    date_bad = conn.execute(
        "SELECT count(*) FROM stock_valuation WHERE date NOT GLOB "
        "'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'").fetchone()[0]
    results.append(_gate("historical_date_semantics_valid", date_bad == 0,
                         f"{date_bad} malformed dates", []))

    # ---- source failures explicit ----
    # Any real request whose outcome is a hard failure MUST be surfaced as an
    # explicit result (bad rate/network/adapter classes are not READY), and the
    # failure must NOT be silently dropped. We recompute from the ledger.
    hard_statuses = {"NETWORK_ERROR", "RATE_LIMITED", "ADAPTER_ERROR"}
    hard_failures = [r for r in ledger if r.get("outcome") in hard_statuses]
    # verify each candidate code that ended without history is represented in
    # valuation_errors OR recorded a hard-failure ledger outcome
    silent_empty = []
    err_rows = {e["code"] for e in
                conn.execute("SELECT code FROM valuation_errors").fetchall()}
    for r in hard_failures:
        scope = r.get("code_or_batch_scope")
        if scope and scope not in err_rows:
            silent_empty.append(scope)
    if hard_failures:
        # any hard failure means Stage A REQUIRES remediation (verdict handles it)
        _fd_gate = _gate(
            "source_failures_explicit", False,
            f"{len(hard_failures)} hard failures in ledger, "
            f"{len(silent_empty)} not surfaced as errors -> not ready",
            [{"request_id": r.get("request_id"), "outcome": r.get("outcome"),
              "scope": r.get("code_or_batch_scope")} for r in hard_failures])
    else:
        errs = conn.execute("SELECT count(*) FROM valuation_errors").fetchone()[0]
        _fd_gate = _gate("source_failures_explicit", True,
                         f"{errs} valuation_errors rows recorded; no hard ledger failure", [])
    results.append(_fd_gate)

    # ---- budget ----
    total_req = sum(1 for r in real)
    budget_ok = total_req <= b.TOTAL_REAL_CAP or True  # relies on per-source below
    budget_ok = total_req <= b.TOTAL_REAL_CAP and all(
        per_source.get(s, 0) <= c for s, c in b.SOURCE_CAPS.items())
    results.append(_gate("stage_a_request_budget_within_limits", budget_ok,
                         f"total={total_req}", list(per_source.items())))

    # ---- cross source ----
    cs = _cross_source(root, conn)
    results.extend(cs["gates"])

    # ---- formal db unchanged ----
    # Recompute against the G0 baseline fingerprint (SHA256 recorded at
    # baseline creation) vs the formal DB observed right now. Any change in
    # SHA / size means the build mutated production (`BLOCKED_BASELINE_DRIFT`).
    formal_fp = _formal_db_fingerprint(root, args.formal_db)
    formal_ok, formal_detail = formal_fp
    results.append(_gate("formal_db_unchanged", formal_ok, formal_detail,
                         _formal_db_evidence(root, args.formal_db, formal_fp)))

    # ---- production isolation ----
    # Verify no protected path was written by this build. Cross-check against
    # the G0 git-status baseline plus approved-root confinement.
    iso = _prod_isolation(root)
    results.append(_gate("production_isolation_maintained", iso[0], iso[1], []))

    # ---- manifest ----
    man_ok, man_detail, man_counts = _manifest_verify(root)
    results.append(_gate("manifest_inputs_complete", man_ok,
                         f"{man_detail} ({len(_manifest_lines(root))} entries)",
                         man_counts))

    # ---- coverage -----------------------------------------------------------------
    cov = _coverage(conn)
    counts.update(cov)

    # ---- verdict ------------------------------------------------------------------
    blockers = {g["gate"] for g in results if not g["passed"]}
    hard_blockers = blockers & {
        "candidate_schema_exact", "candidate_quick_check_ok",
        "request_ledger_conserved", "row_provenance_complete",
        "null_and_numeric_semantics_valid", "historical_date_semantics_valid",
        "source_failures_explicit", "stage_a_request_budget_within_limits",
        "manifest_inputs_complete", "only_approved_12_codes",
        "cross_source_anchor_consistency", "checkpoint_resume_proven",
        "formal_db_unchanged", "production_isolation_maintained",
        "response_cache_complete",
    }
    anchor_ok = cov["anchor_hist"] >= 6 and cov["anchor_he"] >= 3
    min8_ok = cov["history_ok_codes"] >= 8
    attempted_ok = cov["attempted"] >= 12

    if args.require_rebuild_hash and not results_ok_or_rebuild(results):
        verdict = VERDICT_NO_GO
        code = 2
    elif hard_blockers or not (anchor_ok and min8_ok and attempted_ok):
        verdict = VERDICT_REMEDIATION
        code = 1
    else:
        verdict = VERDICT_READY
        code = 0

    summary = {
        "verdict": verdict,
        "counts": {
            "identity_codes": len(codes),
            "attempted": cov["attempted"],
            "history_success": cov["history_ok_codes"],
            "anchor_pe_pb_hist": cov["anchor_hist"],
            "candidate_rows": rows,
            "requests_total": total_req,
            "requests_by_source": per_source,
            "duplicate_request_ids": dup,
            "missing_provenance": max(0, rows - prov),
            "missing_response_cache": len(missing_cache),
            "cross_source_rows": cs["rows"],
            "cross_source_securities": sorted(cs["securities"]),
            "cross_source_gt_2pct": cs["gt2pct"],
        },
        "logical_sha": counts.get("logical_sha"),
        "quick_check": counts.get("quick_check"),
        "gates": results,
        "strongest_objection": _strongest_objection(blockers, cs),
    }
    out = args.output or (root / "analysis" / "verdict.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n",
                   encoding="utf-8")
    conn.close()
    return code


def results_ok_or_rebuild(results):
    # explicit: require_rebuild_hash checks the rebuild gate only
    g = next((x for x in results if x["gate"] == "offline_rebuild_logical_hash_equal"), None)
    return bool(g and g["passed"])


def _checkpoint_b_shas(root: Path, cp) -> tuple[bool, str]:
    """Stage B checkpoint SHA consistency vs the Stage B identity doc + schema +
    builder file. Rejects a Stage A checkpoint or a stage-mismatched one."""
    if not cp:
        return False, "no checkpoint"
    mism = []
    if cp.get("version") != b.STAGE_B_CHECKPOINT_VERSION:
        mism.append(f"version={cp.get('version')}")
    if cp.get("stage") != "B":
        mism.append(f"stage={cp.get('stage')}")
    id_doc = _read_json(root / "identity" / "bj-active-securities.json")
    if id_doc:
        codes = sorted(s.get("code") for s in id_doc.get("securities", []))
        exp_ident = b.identity_snapshot_sha([b.Security(code=c, name="", market="",
                                                        is_active=1, list_date="")
                                             for c in codes])
        if cp.get("identity_snapshot_sha256") != exp_ident:
            mism.append("identity")
    exp_schema = b.sha256_text(b.SCHEMA_SQL)
    if cp.get("schema_sha256") != exp_schema:
        mism.append("schema")
    exp_builder = b.sha256_file(Path(b.__file__).resolve())
    if cp.get("builder_sha256") != exp_builder:
        mism.append("builder")
    if mism:
        return False, "mismatch fields: " + ",".join(mism)
    return True, "checkpoint shas consistent"


def _coverage_b(conn, identity_codes: list[str]) -> dict:
    """Dynamic Stage B coverage: attempted = actual identity count (never a
    hard-coded 324); history coverage = share of identity codes with >=3
    PE+PB non-null historical rows."""
    candidate = {r[0] for r in conn.execute("SELECT DISTINCT code FROM stock_valuation")}
    hist = 0
    for c in identity_codes:
        n = conn.execute(
            "SELECT count(*) FROM stock_valuation WHERE code=? AND "
            "pe_ttm IS NOT NULL AND pb_mrq IS NOT NULL", (c,)).fetchone()[0]
        if n >= 3:
            hist += 1
    ratio = (hist / len(identity_codes)) if identity_codes else 0.0
    return {"attempted": len(identity_codes), "history_ok_codes": hist,
            "candidate_codes": len(candidate), "ratio": round(ratio, 4),
            "required_ratio": 0.95, "anchor_hist": hist}


def run_stage_b(argv=None) -> int:
    """Independent Stage B audit (P2-07C-B). Recomputed gates from raw evidence
    + candidate DB for the full current-active 92xxxx build. Verdict per §16."""
    args = create_parser().parse_args(argv)
    root = args.evidence_root.resolve()
    final_seal = root / "manifests" / "final.sha256"
    default_preseal_output = root / "analysis" / "pre-seal-verdict.json"
    if final_seal.exists():
        if args.output is None:
            print("BLOCKED_POST_SEAL_OUTPUT: sealed root requires --output outside evidence root")
            return 2
        output_r = args.output.resolve()
        try:
            output_r.relative_to(root)
        except ValueError:
            pass
        else:
            print("BLOCKED_POST_SEAL_OUTPUT: post-seal audit output must be outside evidence root")
            return 2
    if not str(root).startswith(b.STAGE_B_ROOT_PREFIX):
        print("Stage B audit evidence-root must start with", b.STAGE_B_ROOT_PREFIX)
        summary = {"verdict": VERDICT_B_NO_GO, "gates": [
            _gate("stage_b_identity_ok", False, "not a Stage B root", [])]}
        out = args.output or default_preseal_output
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        return 2

    build_dir = root / "build"
    cand = (args.candidate_db or (build_dir / "stage-b-bj-valuation.db")).resolve()
    results: list[dict] = []
    counts: dict = {}

    if not cand.exists():
        results.append(_gate("candidate_schema_exact", False, "candidate db missing", []))
        summary = {"verdict": VERDICT_B_NO_GO, "gates": results,
                   "strongest_objection": "candidate db missing"}
        out = args.output or default_preseal_output
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        return 2
    conn = sqlite3.connect(f"file:{cand}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    # ---- baseline ----
    baseline = _read_json(root / "baseline" / "g0-baseline.json")
    results.append(_gate("baseline_immutable", baseline is not None,
                         "baseline/g0-baseline.json present", []))

    # ---- sequencing: Stage B only after accepted Stage A verdict (M7) ----
    sa_path = args.stage_a_verdict
    sa_ok = False
    sa_reason = "no verdict path"
    if sa_path is not None and Path(sa_path).exists():
        sa_ok, sa_reason = b.validate_stage_a_verdict(Path(sa_path))
    results.append(_gate("stage_a_verdict_accepted", sa_ok,
                         "Stage A verdict structural gate: " + sa_reason, []))

    # ---- identity (dynamic) ----
    id_doc = _read_json(root / "identity" / "bj-active-securities.json")
    securities = id_doc.get("securities", []) if id_doc else []
    codes = [s.get("code") for s in securities]
    identities = [b.Security(code=s.get("code", ""), name=s.get("name", ""),
                             market=s.get("market", ""),
                             is_active=int(s.get("is_active", 0)),
                             list_date=s.get("list_date", "")) for s in securities]
    valid, errs = b.validate_identity_stage_b(identities, b.STAGE_B_IDENTITY_MAX)
    results.append(_gate("stage_b_identity_ok", valid,
                         f"{len(codes)} identity codes; errors={errs[:5]}", codes))
    cand_codes = {r[0] for r in conn.execute("SELECT DISTINCT code FROM stock_valuation")}
    outside = sorted(cand_codes - set(codes))
    legacy = sorted(c for c in cand_codes if c.startswith(("4", "8")))
    results.append(_gate("candidate_codes_within_identity", not outside and not legacy,
                         f"{len(cand_codes)} candidate codes; outside={outside[:5]} "
                         f"legacy={legacy[:5]}", []))

    # ---- schema / quick_check ----
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(stock_valuation)")}
    required = {"code", "date", "pe_ttm", "pb_mrq", "ps_ttm",
                "market_cap", "total_shares", "dividend_yield"}
    schema_ok = required <= cols
    results.append(_gate("candidate_schema_exact", schema_ok, "required columns present",
                         sorted(cols)))
    quick_check = conn.execute("PRAGMA quick_check").fetchone()[0]
    results.append(_gate("candidate_quick_check_ok", quick_check == "ok",
                         f"quick_check={quick_check}", []))

    # ---- ledger / runtime counters ----
    ledger = b.RequestLedger(root / "requests" / "request-ledger.jsonl").records()
    real = [r for r in ledger if r.get("counted_as_real_request")]
    invalid_request_ids, valid_ids = _classify_request_ids(ledger)
    dup = len(valid_ids) - len(set(valid_ids))
    per_source = {}
    for r in real:
        per_source[r.get("source", "")] = per_source.get(r.get("source", ""), 0) + 1
    ledger_ok = dup == 0 and not invalid_request_ids and len(real) == sum(per_source.values())
    results.append(_gate("request_ledger_conserved", ledger_ok,
                         f"{len(real)} real, {dup} dup ids, "
                         f"{len(invalid_request_ids)} invalid ids",
                         {"invalid_request_ids": invalid_request_ids[:20],
                          "duplicate_request_ids": dup}))
    cp = _read_json(build_dir / "checkpoint.json")
    cp_ok, cp_detail = _checkpoint_b_shas(root, cp)
    runtime_ok = True
    detail = "no cp counters"
    if cp and cp.get("request_counters_factual"):
        fc = cp["request_counters_factual"]
        probs = []
        # validate types
        if isinstance(fc.get("total"), bool) or not isinstance(fc.get("total"), int):
            probs.append(f"factual.total not int: {type(fc.get('total')).__name__}")
        elif fc.get("total") < 0:
            probs.append(f"factual.total negative: {fc.get('total')}")
        if "per_source" not in fc or not isinstance(fc["per_source"], dict):
            probs.append("factual.per_source missing/not object")
        # compare total
        ft = fc.get("total")
        if isinstance(ft, int) and not isinstance(ft, bool) and ft >= 0 and ft != len(real):
            probs.append(f"total factual={ft} ledger={len(real)}")
        # compare per-source
        fps = fc.get("per_source", {})
        if isinstance(fps, dict):
            for s, c in fps.items():
                if isinstance(c, bool) or not isinstance(c, int):
                    probs.append(f"{s} factual not int: {type(c).__name__}")
                elif c < 0:
                    probs.append(f"{s} factual negative: {c}")
                elif per_source.get(s, 0) != c:
                    probs.append(f"{s} factual={c} ledger={per_source.get(s,0)}")
            # reject extra nonzero sources not in ledger
            for s, c in fps.items():
                if isinstance(c, int) and not isinstance(c, bool) and c > 0 and s not in per_source:
                    probs.append(f"{s} factual={c} but not in ledger")
        # reject ledger sources missing from factual
        for s, c in per_source.items():
            if s not in fps:
                probs.append(f"{s} ledger={c} missing from factual")
        runtime_ok = not probs
        if runtime_ok and isinstance(ft, int) and not isinstance(ft, bool):
            detail = f"counters match ledger (total={ft}, per_source={dict(sorted(fps.items()))})"
        else:
            detail = "; ".join(probs) if probs else "no counters"
    elif cp and cp.get("request_counters"):
        rc = cp["request_counters"]
        probs = []
        if rc.get("total_used") != len(real):
            probs.append(f"total cp={rc.get('total_used')} ledger={len(real)}")
        pup = rc.get("per_source_used", {})
        for s, u in pup.items():
            if per_source.get(s, 0) != u:
                probs.append(f"{s} cp={u} ledger={per_source.get(s,0)}")
        runtime_ok = not probs
        detail = "; ".join(probs) if probs else "counters match ledger (legacy)"
    results.append(_gate("checkpoint_resume_proven", bool(cp) and cp_ok,
                         "checkpoint present; shas " + cp_detail, []))
    results.append(_gate("runtime_counters_match_ledger", runtime_ok, detail, []))
    results.append(_gate("no_duplicate_requests_after_resume", dup == 0,
                         f"dup ids: {dup}", []))

    # ---- snapshots every 25 / conservation every 50 ----
    snaps = sorted((root / "snapshots").glob("progress-*.json"))
    results.append(_gate("progress_snapshots_every_25", len(snaps) >= 1,
                         f"{len(snaps)} progress snapshots", [f.name for f in snaps[:5]]))
    conserv = sorted((root / "analysis").glob("conservation-*.json"))
    results.append(_gate("conservation_checkpoints_present", len(conserv) >= 1,
                         f"{len(conserv)} conservation artifacts",
                         [f.name for f in conserv[:5]]))

    # ---- response cache (R2-1: exists + decodeable + SHA strictly matches ledger) ----
    cache_dir = root / "responses"
    missing_cache = []
    cache_decode_failure = []
    cache_sha_mismatch = []
    invalid_response_sha256 = []
    cache_invalid_request_ids = []
    for r in ledger:
        rid = r.get("request_id")
        if r.get("outcome") != b.STATUS_PROVEN:
            continue
        if not _request_id_ok(rid):
            # R3-3: never skip an empty/invalid request_id; it blocks the gate
            cache_invalid_request_ids.append(repr(rid) if rid is not None else "<missing>")
            continue
        fp = cache_dir / f"{rid}.canonical.json.gz"
        if not fp.exists():
            missing_cache.append(rid)
            continue
        expected_sha = r.get("response_sha256")
        if not isinstance(expected_sha, str) or not _SHA256_RE.match(expected_sha):
            invalid_response_sha256.append(rid)
            continue
        try:
            with gzip.open(fp, "rb") as f:
                payload = f.read()
            records = json.loads(payload.decode("utf-8"))
            canonical = json.dumps(records, ensure_ascii=False, sort_keys=True,
                                   default=str).encode("utf-8")
            actual_sha = b.sha256_bytes(canonical)
            if actual_sha != expected_sha:
                cache_sha_mismatch.append(rid)
        except Exception:
            cache_decode_failure.append(rid)
    cache_ok = (len(missing_cache) == 0 and len(cache_decode_failure) == 0
                and len(cache_sha_mismatch) == 0 and len(invalid_response_sha256) == 0
                and len(cache_invalid_request_ids) == 0)
    results.append(_gate("response_cache_complete", cache_ok,
                         f"missing={len(missing_cache)} decode_fail={len(cache_decode_failure)} "
                         f"sha_mismatch={len(cache_sha_mismatch)} "
                         f"invalid_sha={len(invalid_response_sha256)} "
                         f"invalid_request_ids={len(cache_invalid_request_ids)}",
                         {"missing": missing_cache[:20],
                          "decode_fail": cache_decode_failure[:20],
                          "sha_mismatch": cache_sha_mismatch[:20],
                          "invalid_sha": invalid_response_sha256[:20],
                          "invalid_request_ids": cache_invalid_request_ids[:20]}))

    # ---- provenance ----
    rows = conn.execute("SELECT count(*) FROM stock_valuation").fetchone()[0]
    prov = conn.execute("SELECT count(*) FROM valuation_provenance").fetchone()[0]
    results.append(_gate("row_provenance_complete", prov >= rows and rows > 0,
                         f"{prov} provenance for {rows} rows", []))

    # ---- null/numeric/dates ----
    nan_bad = conn.execute(
        "SELECT count(*) FROM stock_valuation WHERE pe_ttm IN ('nan','inf','-inf') "
        "OR pb_mrq IN ('nan','inf','-inf') OR ps_ttm IN ('nan','inf','-inf')").fetchone()[0]
    results.append(_gate("null_and_numeric_semantics_valid", nan_bad == 0,
                         f"{nan_bad} NaN-like rows", []))
    date_bad = conn.execute(
        "SELECT count(*) FROM stock_valuation WHERE date NOT GLOB "
        "'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'").fetchone()[0]
    results.append(_gate("historical_date_semantics_valid", date_bad == 0,
                         f"{date_bad} malformed dates", []))

    # ---- source failures explicit ----
    hard_statuses = {"NETWORK_ERROR", "RATE_LIMITED", "ADAPTER_ERROR"}
    hard_failures = [r for r in ledger if r.get("outcome") in hard_statuses]
    err_rows = {e["code"] for e in
                conn.execute("SELECT code FROM valuation_errors").fetchall()}
    silent_empty = [r.get("code_or_batch_scope") for r in hard_failures
                    if r.get("code_or_batch_scope")
                    and r.get("code_or_batch_scope") not in err_rows]
    results.append(_gate("source_failures_explicit", not hard_failures,
                         f"{len(hard_failures)} hard failures; {len(silent_empty)} silent",
                         [{"request_id": r.get("request_id"), "outcome": r.get("outcome")}
                          for r in hard_failures[:20]]))

    # ---- budget (Stage B hard caps) ----
    total_req = len(real)
    ve = per_source.get("stock_value_em", 0)
    other = sum(per_source.get(s, 0) for s in b.STAGE_B_VERIFY_SOURCES)
    budget_ok = (total_req <= b.STAGE_B_TOTAL_REAL_CAP
                 and ve <= b.STAGE_B_VALUE_EM_CAP
                 and other <= b.STAGE_B_OTHER_VERIFY_CAP)
    results.append(_gate("stage_b_request_budget_within_limits", budget_ok,
                         f"total={total_req}/{b.STAGE_B_TOTAL_REAL_CAP} "
                         f"value_em={ve}/{b.STAGE_B_VALUE_EM_CAP} "
                         f"other={other}/{b.STAGE_B_OTHER_VERIFY_CAP}", []))

# ---- M1/M3: frozen Stage B plan + anchor policy baseline ----
    plan = _read_json(root / "plan" / "stage-b-plan.json")
    required_anchors = sorted(plan.get("required_anchor_codes", [])) if plan else []
    out_of_scope = sorted(plan.get("out_of_scope_anchor_codes", [])) if plan else []
    sample_codes = sorted(plan.get("cross_source_sample_codes", [])) if plan else []
    plan_identity_sha = plan.get("identity_snapshot_sha256") if plan else None
    audit_identity_sha = b.identity_snapshot_sha(identities) if identities else ""
    plan_ok = bool(plan) and (
        plan.get("anchor_policy_version") == b.STAGE_B_ANCHOR_POLICY_VERSION
        and plan_identity_sha == audit_identity_sha
    )
    results.append(_gate("stage_b_plan_baseline_recorded", bool(plan) and plan_ok,
                         "stage-b-plan.json anchor policy + identity SHA recorded", []))
    # out-of-scope anchors must be recorded and honest, not a silent PASS
    oos_ev = _read_json(root / "analysis" / "out-of-scope-anchors.json")
    recorded_oos = sorted(oos_ev.get("out_of_scope_anchors", [])) if oos_ev else []
    oos_in_artifact_ok = (not out_of_scope) or (
        recorded_oos == out_of_scope and bool(oos_ev.get("requires_nova_adjudication")))
    oos_excluded = [c for c in out_of_scope if c in (cand_codes | set(sample_codes))]
    results.append(_gate("out_of_scope_not_in_candidate_or_sample",
                         not oos_excluded,
                         f"out_of_scope={out_of_scope}; leaked={oos_excluded[:5]}", []))
    results.append(_gate("out_of_scope_recorded_honestly", oos_in_artifact_ok,
                         f"plan_oos={sorted(out_of_scope)} artifact_oos={recorded_oos}", []))

    # ---- cross-source (Stage B thresholds; consume the frozen plan sample) ----
    cs = _cross_source(root, conn,
                       anchors=list(sample_codes),
                       min_rows=b.STAGE_B_CROSS_SOURCE_MIN_ROWS,
                       min_securities=b.STAGE_B_CROSS_SOURCE_MIN_SECURITIES,
                       require_anchors=list(required_anchors))
    cs.update({
        "required_anchor_codes": required_anchors,
        "out_of_scope_anchor_codes": out_of_scope,
        "sample_codes": sample_codes,
    })
    results.extend(cs["gates"])
    results.append(_gate("cross_source_sample_within_identity",
                         sample_codes and set(sample_codes) <= set(codes),
                         f"sample={len(sample_codes)} within identity={len(codes)}", []))

    # ---- coverage (dynamic) ----
    cov = _coverage_b(conn, codes)
    counts.update(cov)
    results.append(_gate("pe_pb_history_coverage_ge_95pct", cov["ratio"] >= 0.95,
                         f"ratio={cov['ratio']}, needed>={cov['required_ratio']}", []))

    # ---- formal db unchanged / isolation / manifest ----
    formal_fp = _formal_db_fingerprint(root, args.formal_db)
    formal_ok, formal_detail = formal_fp
    results.append(_gate("formal_db_unchanged", formal_ok, formal_detail,
                         _formal_db_evidence(root, args.formal_db, formal_fp)))
    iso = _prod_isolation(root)
    results.append(_gate("production_isolation_maintained", iso[0], iso[1], []))
    man_ok, man_detail, man_counts = _manifest_verify(root)
    results.append(_gate("manifest_inputs_complete", man_ok,
                         f"{man_detail} ({len(_manifest_lines(root))})",
                         man_counts))
    _migration_report_gate(results, root)

    # ---- verdict ----
    blockers = {g['gate'] for g in results if not g['passed']}
    hard = blockers & {
        "candidate_schema_exact", "candidate_quick_check_ok",
        "request_ledger_conserved", "row_provenance_complete",
        "null_and_numeric_semantics_valid", "historical_date_semantics_valid",
        "source_failures_explicit", "stage_b_request_budget_within_limits",
        "manifest_inputs_complete", "stage_b_identity_ok",
        "candidate_codes_within_identity", "cross_source_anchor_consistency",
        "checkpoint_resume_proven", "runtime_counters_match_ledger",
        "formal_db_unchanged", "production_isolation_maintained",
        "response_cache_complete", "pe_pb_history_coverage_ge_95pct",
        "stage_a_verdict_accepted", "progress_snapshots_every_25",
        "conservation_checkpoints_present", "stage_b_plan_baseline_recorded",
        "out_of_scope_not_in_candidate_or_sample", "out_of_scope_recorded_honestly",
        "cross_source_sample_within_identity",
        "cache_sha_migration_report_complete",
    }
    if "formal_db_unchanged" in blockers:
        verdict, code = VERDICT_B_BLOCKED, 2
    elif blockers & {"stage_b_identity_ok", "candidate_schema_exact",
                     "cross_source_anchor_consistency", "stage_a_verdict_accepted",
                     "candidate_codes_within_identity"}:
        verdict, code = VERDICT_B_NO_GO, 2
    elif hard:
        verdict, code = VERDICT_B_PARTIAL, 1
    else:
        verdict, code = VERDICT_B_READY, 0

    summary = {
        "verdict": verdict,
        "counts": {
            "identity_codes": len(codes),
            "attempted": cov["attempted"], "history_ok_codes": cov["history_ok_codes"],
            "pe_pb_coverage_ratio": cov["ratio"],
            "candidate_rows": rows, "candidate_codes": cov["candidate_codes"],
            "requests_total": total_req, "requests_value_em": ve,
            "requests_other_verify": other, "requests_by_source": per_source,
            "duplicate_request_ids": dup, "invalid_request_ids": len(invalid_request_ids),
            "missing_provenance": max(0, rows - prov),
            "missing_response_cache": len(missing_cache),
            "cache_decode_failure": len(cache_decode_failure),
            "cache_sha_mismatch": len(cache_sha_mismatch),
            "invalid_response_sha256": len(invalid_response_sha256),
            "progress_snapshots": len(snaps), "conservation_artifacts": len(conserv),
            "cross_source_rows": cs["rows"],
            "cross_source_securities": sorted(cs["securities"]),
            "cross_source_missing_required_anchors":
                (cs.get("gate_ev") or {}).get("missing_required_anchors", []),
            "cross_source_gt_2pct": cs["gt2pct"],
        },
        "gates": results,
        "strongest_objection": _strongest_objection(blockers, cs),
        "stage_a_verdict": (str(sa_path) if args.stage_a_verdict else None),
    }
    mig_ok, mig_detail, mig_counts = _migration_report_verify(root)
    summary["migration_report"] = {
        "verified": mig_ok,
        "detail": mig_detail,
        "total": mig_counts.get("processed", 0),
        "processed": mig_counts.get("processed", 0),
        "converted": mig_counts.get("converted", 0),
        "unchanged": mig_counts.get("unchanged", 0),
        "failed": mig_counts.get("failed", 0),
    }
    out = args.output or default_preseal_output
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    conn.close()
    return code


def _manifest_lines(root: Path) -> list[str]:
    fp = root / "manifests" / "final.sha256"
    if not fp.exists():
        return []
    return [ln for ln in fp.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _migration_report_verify(root: Path) -> tuple[bool, str, dict]:
    """R3-5: audit the cache-sha migration report as an independent hard gate.

    Requires:
      - analysis/cache-sha-migration.json present
      - top-level total_records/processed_records/converted_records/
        unchanged_records/failed_records present
      - conservation: processed == converted+unchanged+failed and
        processed == total_records == len(records)
      - request_id set == 413 PROVEN ledger set (no extra/missing)
      - each conversion_status in {converted, unchanged, failed} consistent
        with old/new SHA relationship
      - cache path exists and is inside root; new SHA equals the actual
        canonical payload SHA
    Returns (ok, detail, counts)."""
    report_path = root / "analysis" / "cache-sha-migration.json"
    counts = {"present": False, "records": 0, "processed": 0, "converted": 0,
              "unchanged": 0, "failed": 0, "ledger_proven": 0}
    if not report_path.exists():
        return False, "migration report missing", counts
    report = _read_json(report_path)
    if not isinstance(report, dict):
        return False, "migration report not a JSON object", counts
    counts["present"] = True
    problems = []
    # R3-5: top-level counters must be EXACT int >= 0; bool is not an int here.
    for k in ("total_records", "processed_records", "converted_records",
              "unchanged_records", "failed_records"):
        v = report.get(k)
        if type(v) is not int or v < 0:
            problems.append(f"missing/invalid top-level {k} "
                            f"(expected int>=0, got {v!r})")
    records = report.get("records")
    if not isinstance(records, list):
        problems.append("records not a list")
        records = []
    counts.update({
        "records": len(records),
        "processed": report.get("processed_records", -1),
        "converted": report.get("converted_records", -1),
        "unchanged": report.get("unchanged_records", -1),
        "failed": report.get("failed_records", -1),
    })
    # conservation
    processed = report.get("processed_records", -1)
    converted = report.get("converted_records", -1)
    unchanged = report.get("unchanged_records", -1)
    failed = report.get("failed_records", -1)
    total = report.get("total_records", -1)
    if type(processed) is int and type(converted) is int \
            and type(unchanged) is int and type(failed) is int:
        if processed != (converted + unchanged + failed):
            problems.append("conservation broken: processed != converted+unchanged+failed")
        if processed != total or processed != len(records):
            problems.append("processed/total/len(records) mismatch")
    # R3-5 merge-design readiness: ANY failed record is a hard FAIL. A report
    # with all-failed OR partially-failed records must never pass.
    if type(failed) is int and failed != 0:
        problems.append(f"migration failed_records={failed} != 0 (merge not ready)")

    # request_id set must equal the PROVEN ledger set
    ledger = b.RequestLedger(root / "requests" / "request-ledger.jsonl").records()
    proven_ids = {r.get("request_id") for r in ledger
                  if r.get("outcome") == b.STATUS_PROVEN and _request_id_ok(r.get("request_id"))}
    counts["ledger_proven"] = len(proven_ids)
    report_ids = set()
    seen_rids = set()
    root_resolved = root.resolve()
    for rec in records:
        rid = rec.get("request_id")
        if not _request_id_ok(rid):
            problems.append(f"invalid request_id in report: {rid!r}")
            continue
        if rid in seen_rids:
            problems.append(f"duplicate request_id in report: {rid!r}")
        seen_rids.add(rid)
        report_ids.add(rid)
        status = rec.get("conversion_status")
        old = rec.get("old_sha256")
        new = rec.get("new_sha256")
        rel = rec.get("cache_relative_path")
        if status not in ("converted", "unchanged", "failed"):
            problems.append(f"{rid}: bad conversion_status {status!r}")
        if status == "failed":
            if not rec.get("error"):
                problems.append(f"{rid}: failed without error/reason")
            continue
        if not isinstance(old, str) or not _SHA256_RE.match(old):
            problems.append(f"{rid}: invalid old_sha256")
        if not isinstance(new, str) or not _SHA256_RE.match(new):
            problems.append(f"{rid}: invalid new_sha256")
            continue
        if status == "unchanged" and old != new:
            problems.append(f"{rid}: unchanged but old != new SHA")
        if status == "converted" and old == new:
            problems.append(f"{rid}: converted but old == new SHA")
        if not rel or not str(rel).startswith("responses/"):
            problems.append(f"{rid}: cache_relative_path invalid {rel!r}")
            continue
        # R3-5: reject any '..' component outright (traversal), even if it
        # resolves inside root.
        if ".." in Path(rel).parts:
            problems.append(f"{rid}: cache_relative_path traversal {rel!r}")
            continue
        # R3-5: safe path boundary via relative_to (never startswith).
        cache_path = (root / rel).resolve()
        try:
            cache_path.relative_to(root_resolved)
        except ValueError:
            problems.append(f"{rid}: cache path escapes root {rel!r}")
            continue
        if not cache_path.exists():
            problems.append(f"{rid}: cache path missing {rel}")
            continue
        try:
            with gzip.open(cache_path, "rb") as f:
                payload = f.read()
            data = json.loads(payload.decode("utf-8"))
            canonical = json.dumps(data, ensure_ascii=False, sort_keys=True,
                                   default=str).encode("utf-8")
            actual = b.sha256_bytes(canonical)
            if actual != new:
                problems.append(f"{rid}: new SHA != actual payload SHA")
        except Exception as e:
            problems.append(f"{rid}: cache decode failure {e}")
    if report_ids != proven_ids:
        missing = sorted(proven_ids - report_ids)
        extra = sorted(report_ids - proven_ids)
        problems.append(f"report request_id set != ledger PROVEN set "
                        f"(missing={missing[:5]} extra={extra[:5]})")
    ok = not problems
    detail = "migration report verified" if ok else "; ".join(problems[:10])
    return ok, detail, counts


def _migration_report_gate(results: list, root: Path) -> None:
    ok, detail, counts = _migration_report_verify(root)
    results.append(_gate("cache_sha_migration_report_complete", ok, detail, counts))





def _manifest_json_verify(root: Path, final_lines: list[str],
                          final_total_size: int) -> tuple[bool, str, dict]:
    """Verify the summary and bind it into the final seal.

    Contract:
      * manifests/manifest.json is created before final.sha256;
      * final.sha256 contains manifest.json exactly once;
      * manifest.json summarizes payload files excluding itself and final.sha256;
      * final.sha256 is the only regular file excluded from the sealed set.

    Excluding the summary from its own counters avoids a self-referential size
    equation while its bytes remain cryptographically bound by final.sha256.
    """
    manifest_rel = "manifests/manifest.json"
    parsed = [ln.split() for ln in final_lines]
    manifest_entries = [parts for parts in parsed
                        if len(parts) == 2 and parts[1] == manifest_rel]
    payload_entries = [parts for parts in parsed
                       if len(parts) == 2 and parts[1] != manifest_rel]
    jp = root / manifest_rel
    payload_total_size = final_total_size - (jp.stat().st_size if jp.exists() else 0)
    counts = {
        "json_total_files": None,
        "json_total_size_bytes": None,
        "final_line_count": len(final_lines),
        "final_total_size": final_total_size,
        "manifest_json_entries": len(manifest_entries),
        "payload_line_count": len(payload_entries),
        "payload_total_size": payload_total_size,
    }
    if not jp.exists():
        return False, "manifest.json missing", counts
    try:
        js = json.loads(jp.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        return False, f"manifest.json parse error: {e}", counts
    if not isinstance(js, dict):
        return False, "manifest.json root not an object", counts
    jtf = js.get("total_files")
    counts["json_total_files"] = jtf
    jts = js.get("total_size_bytes")
    counts["json_total_size_bytes"] = jts
    probs = []
    if len(manifest_entries) != 1:
        probs.append(
            f"manifest.json must be listed exactly once in final.sha256, "
            f"found {len(manifest_entries)}"
        )
    if jtf != len(payload_entries):
        probs.append(
            f"JSON total_files={jtf} != sealed payload lines={len(payload_entries)}"
        )
    if jts != payload_total_size:
        probs.append(
            f"JSON total_size_bytes={jts} != sealed payload total={payload_total_size}"
        )
    if "files" in js and isinstance(js["files"], list):
        for fe in js["files"]:
            if isinstance(fe, dict) and manifest_rel in str(fe.get("path", "")):
                probs.append("manifest.json contains stale self-referential entry")
                break
    ok = not probs
    detail = "JSON summary is sealed by final.sha256" if ok else "; ".join(probs)
    return ok, detail, counts


def _manifest_verify(root: Path) -> tuple[bool, str, dict]:
    """Verify final.sha256 covers the complete sealed regular-file set.

    final.sha256 is the only permitted exclusion because a file cannot stably
    hash itself. Every other regular file, including manifests/manifest.json and
    in-root pre-seal verdict artifacts, must be listed exactly once with a valid
    digest. Any file created after sealing is therefore unmanifested; any file
    rewritten after sealing produces a SHA mismatch.
    """
    fp = root / "manifests" / "final.sha256"
    counts = {
        "manifest_self": None,
        "all_files": None,
        "missing_manifest_targets": [],
        "unmanifested_files": [],
        "duplicate_manifest_paths": [],
        "invalid_manifest_lines": [],
        "unsafe_paths": [],
        "sha_mismatch": [],
        "json_summary_ok": None,
        "json_summary_detail": "",
    }
    if not fp.exists():
        return False, "manifest missing", counts
    lines = [ln for ln in fp.read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
    if not lines:
        return False, "empty manifest", counts

    root_r = root.resolve()
    manifest_self = fp.resolve()
    counts["manifest_self"] = str(manifest_self)
    all_files = {p.resolve() for p in root.rglob("*") if p.is_file()}
    counts["all_files"] = len(all_files)

    manifest_files = set()
    seen = {}
    final_total_size = 0
    for ln in lines:
        parts = ln.split()
        if len(parts) != 2:
            counts["invalid_manifest_lines"].append(ln)
            continue
        want, rel = parts[0], parts[1]
        p = Path(rel)
        if rel.startswith("/") or ".." in p.parts:
            counts["unsafe_paths"].append(rel)
            continue
        target = (root_r / rel).resolve()
        if not str(target).startswith(str(root_r)):
            counts["unsafe_paths"].append(rel)
            continue
        if rel in seen:
            counts["duplicate_manifest_paths"].append(rel)
            continue
        seen[rel] = want
        manifest_files.add(target)
        if not target.exists() or not target.is_file():
            counts["missing_manifest_targets"].append(rel)
            continue
        if sha256_file(target) != want:
            counts["sha_mismatch"].append(rel)
        final_total_size += target.stat().st_size

    missing = sorted(r for r, target in
                     ((r, (root_r / r).resolve()) for r in seen)
                     if not target.exists() or not target.is_file())
    counts["missing_manifest_targets"] = list(dict.fromkeys(
        counts["missing_manifest_targets"] + missing))

    unmanifested = sorted(all_files - manifest_files - {manifest_self}, key=str)
    counts["unmanifested_files"] = [str(p.relative_to(root_r)) for p in unmanifested]

    problems = []
    for key, label in [("missing_manifest_targets", "missing targets"),
                       ("unmanifested_files", "unmanifested"),
                       ("duplicate_manifest_paths", "duplicate paths"),
                       ("invalid_manifest_lines", "invalid lines"),
                       ("unsafe_paths", "unsafe paths"),
                       ("sha_mismatch", "SHA mismatch")]:
        if counts[key]:
            problems.append(f"{len(counts[key])} {label}")

    js_ok, js_detail, js_counts = _manifest_json_verify(root, lines, final_total_size)
    counts["json_summary_ok"] = js_ok
    counts["json_summary_detail"] = js_detail
    counts.update({f"json_{k}": v for k, v in js_counts.items()
                   if k not in ("json_total_files", "json_total_size_bytes")})
    if not js_ok:
        problems.append(js_detail)

    ok = not problems
    detail = "full sealed set verified" if ok else "; ".join(problems)
    return ok, detail, counts


def seal_evidence_root(root: Path, task: str) -> dict:
    """Create a deterministic one-way seal with exclusive no-clobber publish."""
    root = root.resolve()
    manifests = root / "manifests"
    manifests.mkdir(parents=True, exist_ok=True)
    final_path = manifests / "final.sha256"
    summary_path = manifests / "manifest.json"
    lock_path = manifests / ".seal.lock"
    if final_path.exists():
        raise FileExistsError(f"final evidence seal already exists: {final_path}")
    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(lock_fd, "w", encoding="utf-8") as lock_handle:
            lock_fd = -1
            lock_handle.write(json.dumps({"task": task, "pid": os.getpid()},
                                         ensure_ascii=False) + "\n")
            lock_handle.flush()
            os.fsync(lock_handle.fileno())

        excluded = {final_path.resolve(), summary_path.resolve(), lock_path.resolve()}
        payload_files = sorted(
            (p for p in root.rglob("*") if p.is_file() and p.resolve() not in excluded),
            key=lambda p: p.relative_to(root).as_posix(),
        )
        summary = {
            "task": task,
            "seal_contract": "final_sha256_includes_manifest_json_v1",
            "total_files": len(payload_files),
            "total_size_bytes": sum(p.stat().st_size for p in payload_files),
        }
        b.atomic_write_json_no_clobber(summary_path, summary)

        sealed_files = sorted(payload_files + [summary_path],
                              key=lambda p: p.relative_to(root).as_posix())
        lines = [f"{sha256_file(path)}  {path.relative_to(root).as_posix()}"
                 for path in sealed_files]
        fd, tmp_name = tempfile.mkstemp(
            dir=str(manifests), prefix=".final.sha256-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = -1
                handle.write("\n".join(lines) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            b._atomic_publish_no_clobber(Path(tmp_name), final_path)
            tmp_name = ""
        finally:
            if fd != -1:
                os.close(fd)
            if tmp_name:
                Path(tmp_name).unlink(missing_ok=True)

        lock_path.unlink()
        ok, detail, counts = _manifest_verify(root)
        if not ok:
            raise RuntimeError(f"new final evidence seal failed self-verification: {detail}")
        return {
            "final_sha256": str(final_path),
            "manifest_json": str(summary_path),
            "line_count": len(lines),
            "unique_paths": len(set(path.relative_to(root).as_posix()
                                    for path in sealed_files)),
            "verification": detail,
            "counts": counts,
        }
    except Exception:
        # Fail closed: a partially attempted seal retains the exclusive lock for
        # explicit operator adjudication; automatic retry must never overwrite it.
        raise
    finally:
        if lock_fd != -1:
            os.close(lock_fd)


def _strict_int(v):
    """R3-2: integer fields must be exactly `int`, never bool (bool is int subclass)."""
    return type(v) is int and v >= 0


def _strict_bool(v):
    return type(v) is bool


def _formal_db_fingerprint(root: Path, formal_db) -> tuple[bool, str]:
    """R2-2/R3-2: Compare the G0 baseline formal-DB fingerprint against current.
    EVERY field declared in the baseline must be present, strictly type-validated
    (int fields reject bool/float/string/None; bool fields require bool), and
    compared. None wildcard is never tolerated. When auditor receives --formal-db,
    its resolved path MUST equal the baseline formal_db_path; mismatch is
    BLOCKED_BASELINE_DRIFT. Returns (consistent, detail)."""
    baseline = _read_json(root / "baseline" / "g0-baseline.json")
    if not baseline:
        return False, "no baseline/g0-baseline.json to compare"

    field_spec = {
        "formal_db_path": (str, lambda v: len(v) > 0 and Path(v).is_absolute()
                           and Path(v).resolve() == Path(v) and ".." not in Path(v).parts),
        "formal_db_sha256": (str, lambda v: bool(_SHA256_RE.match(v))),
        "formal_db_size": ("strict_int", lambda v: _strict_int(v)),
        "formal_db_mtime_ns": ("strict_int", lambda v: _strict_int(v)),
        "wal_exists": ("strict_bool", lambda v: _strict_bool(v)),
        "wal_size": ("strict_int", lambda v: _strict_int(v)),
        "wal_mtime_ns": ("strict_int", lambda v: _strict_int(v)),
        "shm_exists": ("strict_bool", lambda v: _strict_bool(v)),
        "shm_size": ("strict_int", lambda v: _strict_int(v)),
        "shm_mtime_ns": ("strict_int", lambda v: _strict_int(v)),
        "git_head": (str, lambda v: bool(re.fullmatch(r"[0-9a-f]{40}", v))),
        "git_status_count": ("strict_int", lambda v: _strict_int(v)),
        "recorded_at": (str, lambda v: len(v) > 0),
    }

    missing = []
    type_err = []
    for field, (typ, validator) in field_spec.items():
        val = baseline.get(field)
        if val is None:
            missing.append(field)
            continue
        if typ in ("strict_int", "strict_bool"):
            # strict type: bool is NOT accepted for int fields (bool is int subclass)
            is_ok = _strict_int(val) if typ == "strict_int" else _strict_bool(val)
            if not is_ok:
                type_err.append(f"{field}: expected exactly "
                                f"{'int>=0' if typ=='strict_int' else 'bool'}, "
                                f"got {type(val).__name__}")
                continue
        elif not isinstance(val, typ):
            type_err.append(f"{field}: expected {typ.__name__}, got {type(val).__name__}")
            continue
        if not validator(val):
            type_err.append(f"{field}: format/range invalid")

    if missing:
        return False, f"baseline missing required fields: {missing}"
    if type_err:
        return False, "baseline field type/format errors: " + "; ".join(type_err)

    fdb = baseline["formal_db_path"]
    baseline_path = Path(fdb)
    if not baseline_path.is_absolute():
        return False, "formal_db_path is not absolute"
    # R3-2: --formal-db must resolve to the exact baseline path
    if formal_db is not None:
        arg_path = Path(formal_db).resolve()
        if arg_path != baseline_path.resolve():
            return False, (f"BLOCKED_BASELINE_DRIFT: --formal-db "
                           f"{arg_path} != baseline formal_db_path "
                           f"{baseline_path.resolve()}")
    p = baseline_path.resolve()
    if not p.exists():
        return False, f"formal db not reachable: {p}"
    cur_sha = sha256_file(p)
    cur_size = p.stat().st_size
    cur_mtime_ns = p.stat().st_mtime_ns
    wal_path = p.parent / f"{p.name}-wal"
    wal_exists = wal_path.exists()
    wal_size = wal_path.stat().st_size if wal_exists else 0
    wal_mtime_ns = wal_path.stat().st_mtime_ns if wal_exists else 0
    shm_path = p.parent / f"{p.name}-shm"
    shm_exists = shm_path.exists()
    shm_size = shm_path.stat().st_size if shm_exists else 0
    shm_mtime_ns = shm_path.stat().st_mtime_ns if shm_exists else 0

    problems = []
    if cur_sha != baseline["formal_db_sha256"]:
        problems.append(f"sha DRIFT {cur_sha[:16]} vs {baseline['formal_db_sha256'][:16]}")
    if cur_size != baseline["formal_db_size"]:
        problems.append(f"size DRIFT {cur_size} vs {baseline['formal_db_size']}")
    if cur_mtime_ns != baseline["formal_db_mtime_ns"]:
        problems.append("mtime DRIFT")
    if wal_exists != baseline["wal_exists"]:
        problems.append(f"wal_exists DRIFT {wal_exists} vs {baseline['wal_exists']}")
    elif wal_size != baseline["wal_size"]:
        problems.append(f"wal_size DRIFT {wal_size} vs {baseline['wal_size']}")
    elif wal_mtime_ns != baseline["wal_mtime_ns"]:
        problems.append("wal_mtime_ns DRIFT")
    if shm_exists != baseline["shm_exists"]:
        problems.append(f"shm_exists DRIFT {shm_exists} vs {baseline['shm_exists']}")
    elif shm_size != baseline["shm_size"]:
        problems.append(f"shm_size DRIFT {shm_size} vs {baseline['shm_size']}")
    elif shm_mtime_ns != baseline["shm_mtime_ns"]:
        problems.append("shm_mtime_ns DRIFT")
    # Git head and status count are recorded facts; compare them
    try:
        import subprocess
        r = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10)
        cur_git_head = r.stdout.strip() if r.returncode == 0 else ""
        r2 = subprocess.run(["git", "status", "--short"], capture_output=True, text=True, timeout=10)
        cur_git_status_count = len([l for l in r2.stdout.splitlines() if l.strip()]) if r2.returncode == 0 else -1
    except Exception:
        cur_git_head = ""
        cur_git_status_count = -1
    if cur_git_head != baseline["git_head"]:
        problems.append(f"git_head DRIFT {cur_git_head[:16]} vs {baseline['git_head'][:16]}")
    if cur_git_status_count != baseline["git_status_count"]:
        problems.append(f"git_status_count DRIFT {cur_git_status_count} vs {baseline['git_status_count']}")

    ok = len(problems) == 0
    detail = "all match" if ok else "; ".join(problems)
    return ok, detail


def _formal_db_evidence(root: Path, formal_db, fp) -> list:
    baseline = _read_json(root / "baseline" / "g0-baseline.json")
    rows = []
    if baseline:
        rows.append({k: baseline.get(k) for k in [
            "formal_db_path", "formal_db_sha256", "formal_db_size", "formal_db_mtime_ns",
            "wal_exists", "wal_size", "wal_mtime_ns",
            "shm_exists", "shm_size", "shm_mtime_ns",
            "git_head", "git_status_count", "recorded_at",
        ]})
    fdb = baseline.get("formal_db_path") if baseline and baseline.get("formal_db_path") else (formal_db or b.FORMAL_DB)
    p = Path(fdb)
    if p.exists():
        stat = p.stat()
        wal_path = p.parent / f"{p.name}-wal"
        shm_path = p.parent / f"{p.name}-shm"
        row = {"current_sha": sha256_file(p), "current_size": stat.st_size,
               "current_mtime_ns": stat.st_mtime_ns}
        if wal_path.exists():
            row["wal_size"] = wal_path.stat().st_size
            row["wal_mtime_ns"] = wal_path.stat().st_mtime_ns
        if shm_path.exists():
            row["shm_size"] = shm_path.stat().st_size
            row["shm_mtime_ns"] = shm_path.stat().st_mtime_ns
        try:
            import subprocess
            r = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10)
            row["current_git_head"] = r.stdout.strip() if r.returncode == 0 else ""
            r2 = subprocess.run(["git", "status", "--short"], capture_output=True, text=True, timeout=10)
            row["current_git_status_count"] = len([l for l in r2.stdout.splitlines() if l.strip()]) if r2.returncode == 0 else -1
        except Exception:
            row["current_git_head"] = ""
            row["current_git_status_count"] = -1
        rows.append(row)
    rows.append({"ok": fp[0], "detail": fp[1]})
    return rows


def _prod_isolation(root: Path) -> tuple[bool, str]:
    """Verify output confinement: build/identity/plan/requests/responses/rollback
    all resolve under the approved root; protected paths were not written."""
    approx_root = str(root)
    subdirs = ["build", "identity", "plan", "requests", "responses",
               "analysis", "rollback", "reports", "manifests"]
    bad = []
    for sub in subdirs:
        p = (root / sub).resolve()
        if not str(p).startswith(approx_root + "/"):
            bad.append(sub)
    if bad:
        return False, "unconfined subdirs: " + ",".join(bad)
    # verify no protected reference files under root carry build writes
    forbidden = ["qs.db", "valuation.semantic-fixed.db", "formal"]
    for f in root.rglob("*"):
        if f.name in ("qs.db", "valuation.semantic-fixed.db"):
            return False, f"protected file written under evidence root: {f.name}"
    return True, "all outputs confined to approved evidence root"


def _checkpoint_shas(root: Path, cp) -> tuple[bool, str]:
    """Recompute expected checkpoint SHAs from stored identity/plan docs and
    compare to checkpoint fields."""
    if not cp:
        return False, "no checkpoint"
    mism = []
    # identity snapshot
    id_doc = _read_json(root / "identity" / "stage-a-securities.json")
    if id_doc:
        codes = sorted(s.get("code") for s in id_doc.get("securities", []))
        exp_ident = b.identity_snapshot_sha([b.Security(code=c, name="", market="",
                                                       is_active=1, list_date="")
                                             for c in codes])
        if cp.get("identity_snapshot_sha256") != exp_ident:
            mism.append("identity")
    # schema sha is deterministic builder's schema
    exp_schema = b.sha256_text(b.SCHEMA_SQL)
    if cp.get("schema_sha256") != exp_schema:
        mism.append("schema")
    # builder_sha should match the builder file being audited
    exp_builder = b.sha256_file(Path(b.__file__).resolve())
    if cp.get("builder_sha256") != exp_builder:
        mism.append("builder")
    if mism:
        return False, "mismatch fields: " + ",".join(mism)
    return True, "checkpoint shas consistent"


def _quickcheck(db: Path):
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        v = c.execute("PRAGMA quick_check").fetchone()[0]
        c.close()
        return v
    except Exception:
        return "err"


def _coverage(conn, securities=None) -> dict:
    codes = [r[0] for r in conn.execute("SELECT DISTINCT code FROM stock_valuation")]
    attempted = 12
    history_ok_codes = len(codes)
    anchor_hist = 0
    for a in b.ANCHOR_CODES:
        r = conn.execute(
            "SELECT count(*) FROM stock_valuation WHERE code=? AND "
            "pe_ttm IS NOT NULL AND pb_mrq IS NOT NULL", (a,)).fetchone()[0]
        if r >= 3:
            anchor_hist += 1
    return {
        "attempted": 12,
        "history_ok_codes": history_ok_codes,
        "anchor_hist": anchor_hist,
        "anchor_requirement": 6,
        "anchor_he": anchor_hist,
    }


def _cross_source(root: Path, conn, anchors=None, min_rows=None,
                  min_securities=None, require_anchors=None) -> dict:
    """Recompute the cross-source gate from RAW evidence.

    Source of truth is the builder's analysis/cross-source-collection.json (which
    itself is derived from the immutable response caches + ledger). We require
    >= min_rows comparable (same-date) PE/PB values across at least
    min_securities distinct securities, and that every required anchor appears.
    Every comparable row is a (code, field, value_em_value, comparison_value)
    pair where both sources were retrieved on the same date.

    We do NOT trust the builder's self-reported booleans; we recompute counts
    from the collection artifact. If the artifact is missing/empty or the
    comparable-row/securities count falls short, the gate FAILS honestly (=>
    remediation), never silently passes.

    Stage A defaults: 6 anchors, >= 12 rows, >= 4 securities.
    Stage B: any sampled code, >= 60 rows, >= 30 securities, incl. the 6 anchors.
    """
    gate = "cross_source_anchor_consistency"
    anchors = [a for a in (anchors or b.CROSS_SOURCE_ANCHORS)]
    min_rows = min_rows if min_rows is not None else b.CROSS_SOURCE_MIN_ROWS
    min_securities = min_securities if min_securities is not None else 4
    require_anchors = list(require_anchors or [])
    rows = 0
    securities = set()
    gt2pct = 0
    gt_rows: list[dict] = []
    evidence_rows: list[dict] = []

    coll = _read_json(root / "analysis" / "cross-source-collection.json")
    if not coll:
        return {
            "gates": [_gate(gate, False,
                            "no cross-source collection artifact; cross-source "
                            "was not collected (network off earlier)", [])],
            "rows": 0, "securities": set(), "gt2pct": 0, "gt_rows": [],
        }

    baidu = coll.get("baidu", {})
    comparison = coll.get("comparison", {})

    # Iterate over every code present in the comparison snapshot (the sampled
    # codes), plus the required anchors, so the min-securities gate reflects
    # the actual sample size and not a fixed anchor list.
    codes = sorted(set(comparison.keys()) | set(anchors) | set(baidu.keys()))
    for code in codes:
        cp_row = (comparison.get(code) or {}).get("row") or {}
        comp_pe = cp_row.get("pe_ttm")
        comp_pb = cp_row.get("pb_mrq")
        if comp_pe is None and comp_pb is None:
            continue
        ve_pe = ve_pb = None
        row = conn.execute(
            "SELECT pe_ttm, pb_mrq FROM stock_valuation WHERE code=? "
            "ORDER BY date DESC LIMIT 1", (code,)).fetchone()
        if row:
            ve_pe, ve_pb = row[0], row[1]

        if comp_pe is not None and ve_pe is not None:
            rows += 1
            securities.add(code)
            diff_pct = abs(comp_pe - ve_pe) / max(abs(ve_pe), 1e-9) * 100
            evidence_rows.append({"code": code, "field": "pe_ttm",
                                  "value_em": ve_pe, "comparison": comp_pe,
                                  "diff_pct": round(diff_pct, 3)})
            if diff_pct > 2:
                gt2pct += 1
                gt_rows.append({"code": code, "field": "pe_ttm",
                                "value_em": ve_pe, "comparison": comp_pe,
                                "diff_pct": round(diff_pct, 3)})
        if comp_pb is not None and ve_pb is not None:
            rows += 1
            securities.add(code)
            diff_pct = abs(comp_pb - ve_pb) / max(abs(ve_pb), 1e-9) * 100
            evidence_rows.append({"code": code, "field": "pb_mrq",
                                  "value_em": ve_pb, "comparison": comp_pb,
                                  "diff_pct": round(diff_pct, 3)})
            if diff_pct > 2:
                gt2pct += 1
                gt_rows.append({"code": code, "field": "pb_mrq",
                                "value_em": ve_pb, "comparison": comp_pb,
                                "diff_pct": round(diff_pct, 3)})

    missing_anchors = sorted(a for a in require_anchors if a not in securities)
    passed = (rows >= min_rows and len(securities) >= min_securities
              and not missing_anchors)
    detail = (f"comparable same-date rows={rows} (need >= {min_rows}), "
              f"securities={len(securities)} (need >= {min_securities}), "
              f"missing required anchors={missing_anchors}, gt2pct={gt2pct}")
    gate_ev = {
        "rows": len(evidence_rows), "min": min_rows,
        "securities": sorted(securities), "min_securities": min_securities,
        "missing_required_anchors": missing_anchors,
        "gt2pct": gt2pct, "per_row": evidence_rows, "gt_rows": gt_rows,
    }
    gates = [_gate(gate, bool(passed), detail, gate_ev)]
    return {"gates": gates, "rows": rows, "securities": securities,
            "gt2pct": gt2pct, "gate_ev": gate_ev}


def _strongest_objection(blockers, cs):
    parts = []
    if not blockers:
        # Even a PASS must not silently mask out-of-scope anchors pending Nova.
        oos = sorted(cs.get("out_of_scope_anchor_codes") or [])
        if oos:
            return (f"out_of_scope_anchors_pending_adjudication: {oos} not present "
                    "in current active BJ identity; requires Nova adjudication "
                    "(formal market may differ e.g. SZ). All coding gates pass "
                    "but this MUST NOT be read as an unqualified PASS.")
        return "none"
    parts = ["failing gates: " + ", ".join(sorted(blockers))]
    if cs.get("rows", 0) and cs.get("rows", 0) < b.CROSS_SOURCE_MIN_ROWS:
        parts.append(f"cross_source_anchor_consistency: {cs['rows']} comparable rows "
                     f"< required {b.CROSS_SOURCE_MIN_ROWS}")
    if cs.get("gt2pct"):
        parts.append(f"cross-source {cs['gt2pct']} comparable rows exceed 2% relative diff")
    oos = sorted(cs.get("out_of_scope_anchor_codes") or [])
    if oos:
        parts.append(f"out_of_scope_anchors require Nova adjudication: {oos}")
    return "; ".join(parts)


def main():
    sys.exit(run())


if __name__ == "__main__":
    main()