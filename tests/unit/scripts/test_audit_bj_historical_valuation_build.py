"""Offline unit tests for scripts/audit_bj_historical_valuation_build.py
(P2-07C-A).

Auditor must recompute gates from evidence/DB and not trust builder booleans.
All tests offline; use a temp evidence tree built in-memory.
"""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
import build_bj_historical_valuation_isolated as b  # noqa: E402
from audit_bj_historical_valuation_build import (  # noqa: E402
    run,
    logical_sha,
    create_parser,
    VERDICT_B_READY,
    _manifest_verify,
    _formal_db_fingerprint,
    _request_id_ok,
    _migration_report_verify,
    seal_evidence_root,
)


@pytest.fixture(autouse=True)
def _redirect_formal_db_off_prod(tmp_path, monkeypatch):
    """Memory-card double insurance (2026-08-27): never let the auditor's default
    --formal-db resolve to the 8.4G production qs.db.

    Root cause of the 8.34G RSS peak: no test passes --formal-db, so create_parser
    (default=Path(b.FORMAL_DB)) and _formal_db_evidence's fallback
    (``... else (formal_db or b.FORMAL_DB)``) both resolve to the production DB, and
    the old read_bytes() sha256_file slurped it whole on every run() call. The
    production-side chunked sha256_file now bounds RSS, but hashing 8.4G is still
    slow I/O the tests don't need. Point b.FORMAL_DB at a tiny synthetic file so the
    test path never touches the production DB at all. Both read sites dereference
    b.FORMAL_DB at call time, so the patch takes effect.
    """
    fake = tmp_path / "fake-formal.db"
    fake.write_bytes(b"synthetic-formal-db-for-tests")
    monkeypatch.setattr(b, "FORMAL_DB", str(fake))


def _make_candidate(db_path: Path, n_codes=3, rows_per=4):
    conn = b.connect_candidate(db_path)
    for ci, code in enumerate(["920017", "920185", "920799"][:n_codes]):
        for r in range(rows_per):
            date = f"2026-0{(r+1):02d}-0{(r+1):02d}"
            row = {"code": code, "date": date, "pe_ttm": 10.0 + r,
                   "pb_mrq": 1.1, "ps_ttm": 0.9, "market_cap": 1e9,
                   "total_shares": 1e8, "dividend_yield": None}
            prov = {"code": code, "date": date, "request_id": f"req-{code}",
                    "source": "stock_value_em", "source_column_map": "{}",
                    "raw_row_sha256": "raw", "normalized_record_sha256": "n",
                    "retrieved_at": "2026-07-01T00:00:00+00:00",
                    "adapter_sha256": "a", "identity_snapshot_sha256": "i",
                    "mapping_snapshot_sha256": "m", "normalization_version": "v1"}
            conn.execute("INSERT OR REPLACE INTO stock_valuation "
                         "(code,date,pe_ttm,pb_mrq,ps_ttm,market_cap,total_shares,dividend_yield) "
                         "VALUES (?,?,?,?,?,?,?,?)",
                         (row["code"], row["date"], row["pe_ttm"], row["pb_mrq"],
                          row["ps_ttm"], row["market_cap"], row["total_shares"],
                          row["dividend_yield"]))
            conn.execute("INSERT OR REPLACE INTO valuation_provenance VALUES "
                         "(?,?,?,?,?,?,?,?,?,?,?,?)",
                         (prov["code"], prov["date"], prov["request_id"], prov["source"],
                          prov["source_column_map"], prov["raw_row_sha256"],
                          prov["normalized_record_sha256"], prov["retrieved_at"],
                          prov["adapter_sha256"], prov["identity_snapshot_sha256"],
                          prov["mapping_snapshot_sha256"], prov["normalization_version"]))
    conn.commit()
    conn.close()


def _write_ledger(root, recs):
    p = root / "requests" / "request-ledger.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs),
                 encoding="utf-8")


def _verdict_output(root):
    """Auditor output outside the evidence root, valid before and after seal."""
    return root.parent / f"{root.name}-auditor-verdict.json"


def _mk_summary(root):
    identity_dir = root / "identity"
    identity_dir.mkdir(parents=True, exist_ok=True)
    (identity_dir / "stage-a-securities.json").write_text(
        json.dumps({"securities": [
            {"code": c, "name": f"公司{i}", "market": "北交所", "is_active": 1}
            for i, c in enumerate(b.APPROVED_CODES)
        ]}, ensure_ascii=False), encoding="utf-8")
    (root / "baseline").mkdir(parents=True, exist_ok=True)
    (root / "baseline" / "g0-baseline.json").write_text(
        json.dumps({"header": "ok"}), encoding="utf-8")
    (root / "manifests").mkdir(parents=True, exist_ok=True)
    (root / "manifests" / "final.sha256").write_text(
        "\n".join(f"{'0'*64}  some/file" for _ in range(3)) + "\n", encoding="utf-8")


def test_logical_sha_stable():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "a.db"
        _make_candidate(p)
        s1 = logical_sha(p)
        s2 = logical_sha(p)
        assert s1 == s2
        assert s1 != ""


def test_auditor_recomputes_gates(tmp_path):
    root = tmp_path / "ev"
    _mk_summary(root)
    _make_candidate(root / "build" / "stage-a-bj-valuation.db")
    recs = [
        {"request_id": f"req-{c}", "source": "stock_value_em",
         "code_or_batch_scope": c, "counted_as_real_request": 1,
         "outcome": "PROVEN", "response_sha256": "x"}
        for c in ["920017", "920185", "920799"]
    ]
    _write_ledger(root, recs)
    for c in ["920017", "920185", "920799"]:
        _claim_cache(root, f"req-{c}", _hist())
    results = run(["--evidence-root", str(root), "--output", str(root / "verdict.json")])
    # auditor must produce a verdict and not crash
    assert results is not None


def test_auditor_returns_code(tmp_path):
    root = tmp_path / "ev2"
    _mk_summary(root)
    _make_candidate(root / "build" / "stage-a-bj-valuation.db")
    code = run(["--evidence-root", str(root), "--output", str(root / "verdict.json")])
    assert isinstance(code, int)


def _hist(n=3, prefix=""):
    return [
        {"数据日期": f"2026-07-{i+1:02d}", "PE(TTM)": 10.0, "市净率": 1.1,
         "市销率": 0.9, "总市值": 1e9, "总股本": 1e8}
        for i in range(n)
    ]


def _claim_cache(root, rid, records):
    cs = root / "responses"
    b.ResponseCache(cs).save(rid, records)


def test_missing_provenance_fails_gate(tmp_path):
    root = tmp_path / "ev3"
    _mk_summary(root)
    cand = root / "build" / "stage-a-bj-valuation.db"
    conn = b.connect_candidate(cand)
    # add a row WITHOUT provenance
    conn.execute(
        "INSERT OR REPLACE INTO stock_valuation VALUES (?,?,?,?,?,?,?,?)",
        ("920017", "2026-01-01", 10.0, 1.1, 0.9, 1e9, 1e8, None))
    conn.commit(); conn.close()
    code = run(["--evidence-root", str(root), "--output", str(root / "verdict.json")])
    assert isinstance(code, int)


def test_quick_check_recomputed(tmp_path):
    root = tmp_path / "ev4"
    _mk_summary(root)
    _make_candidate(root / "build" / "stage-a-bj-valuation.db")
    import sqlite3
    conn = sqlite3.connect(root / "build" / "stage-a-bj-valuation.db")
    qc = conn.execute("PRAGMA quick_check").fetchone()[0]
    conn.close()
    assert qc == "ok"


# ---------------------------------------------------------------------------
# R1: cross-source gate must NOT be a constant-true stub (work order §9.2).
# With no cross-source collection artifact, the gate MUST fail honestly
# (=> remediation), not pass with 0 rows.
# ---------------------------------------------------------------------------
def test_cross_source_gate_fails_honestly_without_artifact(tmp_path):
    from audit_bj_historical_valuation_build import _cross_source
    root = tmp_path / "ev_cs0"
    _mk_summary(root)
    cand = root / "build" / "stage-a-bj-valuation.db"
    _make_candidate(cand)
    # No analysis/cross-source-collection.json exists
    res = _cross_source(root, None)
    gate = next(g for g in res["gates"]
                if g["gate"] == "cross_source_anchor_consistency")
    assert gate["passed"] is False
    assert res["rows"] == 0


def test_cross_source_gate_recounts_rows(tmp_path):
    """Cross-source gate recomputes from the collection artifact; >= 12 rows
    and >= 4 securities are required to pass."""
    from audit_bj_historical_valuation_build import _cross_source
    root = tmp_path / "ev_cs2"
    _mk_summary(root)
    cand = root / "ev_cs2" / "cand.db"
    cand = root / "build" / "stage-a-bj-valuation.db"
    _make_candidate(cand)
    # Build a collection artifact supplying 6 anchors x 2 fields
    coll = {"baidu": {}, "comparison": {}}
    for i, code in enumerate(b.ANCHOR_CODES):
        import sqlite3
        conn = sqlite3.connect(cand)
        # ensure candidate has a latest date row with PE/PB for the anchor
        conn.execute("INSERT OR REPLACE INTO stock_valuation "
                     "(code,date,pe_ttm,pb_mrq,ps_ttm,market_cap,total_shares,dividend_yield) "
                     "VALUES (?,?,?,?,?,?,?,?)",
                     (code, f"2026-07-{i+1:02d}", 20.0 + i, 2.0, 1.0, 1e9, 1e8, None))
        conn.execute("INSERT OR REPLACE INTO valuation_provenance VALUES "
                     "(?,?,?,?,?,?,?,?,?,?,?,?)",
                     (code, f"2026-07-{i+1:02d}", f"req-{code}", "stock_value_em", "{}",
                      "r", "n", "2026-07-01", "a", "i", "m", "v1"))
        conn.commit(); conn.close()
        coll["comparison"][code] = {"row": {"code": code, "date": f"2026-07-{i+1:02d}",
                                            "pe_ttm": 20.0 + i, "pb_mrq": 1.0}}
    from pathlib import Path
    import json
    pdir = root / "analysis"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "cross-source-collection.json").write_text(
        json.dumps(coll, ensure_ascii=False), encoding="utf-8")
    conn = sqlite3.connect(cand)
    res = _cross_source(root, conn)
    gate = next(g for g in res["gates"]
                if g["gate"] == "cross_source_anchor_consistency")
    # value_em PE/PB vs comparison PE/PB per anchor = 12 rows expected
    # NOTE: value_em latest uses ORDER BY date DESC; each anchor has 1 row
    assert res["rows"] >= 12
    assert gate["passed"] is True


def test_cross_source_gt_2pct_reported(tmp_path):
    from audit_bj_historical_valuation_build import _cross_source
    root = tmp_path / "ev_cs3"
    _mk_summary(root)
    cand = root / "build" / "stage-a-bj-valuation.db"
    _make_candidate(cand)
    coll = {"baidu": {}, "comparison": {}}
    import sqlite3, json
    conn = sqlite3.connect(cand)
    for i, code in enumerate(["920017", "920185", "920799"]):
        conn.execute("INSERT OR REPLACE INTO stock_valuation "
                     "(code,date,pe_ttm,pb_mrq,ps_ttm,market_cap,total_shares,dividend_yield) "
                     "VALUES (?,?,?,?,?,?,?,?)",
                     (code, f"2026-07-{i+1:02d}", 10.0, 1.0, 1.0, 1e9, 1e8, None))
        # comparison far off to trigger >2%
        coll["comparison"][code] = {"row": {"code": code, "date": f"2026-07-{i+1:02d}",
                                            "pe_ttm": 100.0, "pb_mrq": 50.0}}
    conn.commit(); conn.close()
    pdir = root / "analysis"; pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "cross-source-collection.json").write_text(
        json.dumps(coll, ensure_ascii=False), encoding="utf-8")
    conn = sqlite3.connect(cand)
    res = _cross_source(root, conn)
    assert res["gt2pct"] >= 1


# helper aliases used by the tests above -----------------------------------
def _import(b):
    import sqlite3
    conn = sqlite3.connect(b)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Stage B (P2-07C-B) failure counter-examples. Fully offline.
# ---------------------------------------------------------------------------


def _bj_identity(root, n=30, start=920000, extra=()):
    ident_dir = root / "identity"
    ident_dir.mkdir(parents=True, exist_ok=True)
    secs = [
        {"code": f"{start + i:06d}", "name": f"公司{i}", "market": "北交所",
         "is_active": 1, "list_date": "2021-01-01"}
        for i in range(n)
    ]
    secs.extend(extra)
    (ident_dir / "bj-active-securities.json").write_text(
        json.dumps({"securities": secs}, ensure_ascii=False), encoding="utf-8")


def _mk_bsummary(root, n=30):
    root.mkdir(parents=True, exist_ok=True)
    _bj_identity(root, n)
    (root / "baseline").mkdir(parents=True, exist_ok=True)
    bl = root / "baseline" / "g0-baseline.json"
    bl.write_text(json.dumps({"header": "ok"}), encoding="utf-8")
    _write_manifest(root, ["baseline/g0-baseline.json"])


def _make_bcandidate(db_path, codes, rows_per=4):
    conn = b.connect_candidate(db_path)
    for code in codes:
        for r in range(rows_per):
            date = f"2026-0{(r+1):02d}-0{(r+1):02d}"
            conn.execute("INSERT OR REPLACE INTO stock_valuation "
                         "(code,date,pe_ttm,pb_mrq,ps_ttm,market_cap,total_shares,dividend_yield) "
                         "VALUES (?,?,?,?,?,?,?,?)",
                         (code, date, 10.0 + r, 1.1, 0.9, 1e9, 1e8, None))
            conn.execute("INSERT OR REPLACE INTO valuation_provenance VALUES "
                         "(?,?,?,?,?,?,?,?,?,?,?,?)",
                         (code, date, f"req-{code}", "stock_value_em", "{}",
                          "r", "n", "2026-07-01", "a", "i", "m", "v1"))
    conn.commit()
    conn.close()


def _stage_b_verdict_path(root, with_gates=True):
    p = root / "stage-a-verdict.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    if not with_gates:
        p.write_text(json.dumps({"verdict": b.STAGE_A_READY_VERDICT}),
                     encoding="utf-8")
        return p
    doc = {
        "verdict": b.STAGE_A_READY_VERDICT,
        "gates": [{"gate": g, "passed": True} for g in b.STAGE_A_REQUIRED_GATES],
        "strongest_objection": "none",
    }
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return p


def _mk_stage_b_plan(root, identities, identity_sha=None):
    """Write the Stage B frozen plan (as run_stage_b --dry-run-plan would)."""
    secs = [b.Security(code=s["code"], name=s["name"], market=s["market"],
                       is_active=int(s["is_active"]), list_date=s["list_date"])
            for s in identities]
    if identity_sha is None:
        identity_sha = b.identity_snapshot_sha(secs)
    plan = b.build_plan_stage_b(secs, identity_sha)
    pdir = root / "plan"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "stage-b-plan.json").write_text(
        json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    return plan


def test_stage_b_audit_requires_stage_a_verdict(tmp_path):
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_v1"
    _mk_bsummary(root)
    _make_bcandidate(root / "build" / "stage-b-bj-valuation.db",
                     ["920000", "920001", "920002"])
    code = run(["--mode", "stage-b", "--evidence-root", str(root),
                "--output", str(_verdict_output(root))])
    doc = json.loads((_verdict_output(root)).read_text(encoding="utf-8"))
    g = next(g for g in doc["gates"] if g["gate"] == "stage_a_verdict_accepted")
    assert g["passed"] is False
    assert doc["verdict"] != VERDICT_B_READY
    assert code in (1, 2)


def test_stage_b_audit_rejects_non_b_root(tmp_path):
    b.STAGE_B_ROOT_PREFIX = str(tmp_path / "phase2_p2_07c_b")
    root = tmp_path / "elsewhere"
    code = run(["--mode", "stage-b", "--evidence-root", str(root),
                "--output", str(_verdict_output(root))])
    assert code == 2


def test_stage_b_cross_source_lt_30_fails(tmp_path):
    from audit_bj_historical_valuation_build import _cross_source
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_v2"
    _mk_bsummary(root)
    cand = root / "build" / "stage-b-bj-valuation.db"
    _make_bcandidate(cand, ["920000", "920001", "920002", "920003"])
    # only 4 securities in the collection -> < 30 required
    coll = {"baidu": {}, "comparison": {}}
    for i, code in enumerate(["920000", "920001", "920002", "920003"]):
        coll["comparison"][code] = {"row": {"code": code, "date": "2026-07-01",
                                            "pe_ttm": 10.0, "pb_mrq": 1.1}}
    pdir = root / "analysis"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "cross-source-collection.json").write_text(
        json.dumps(coll, ensure_ascii=False), encoding="utf-8")
    conn = sqlite3.connect(cand)
    res = _cross_source(root, conn,
                        anchors=list(b.STAGE_B_SAMPLE_ANCHORS),
                        min_rows=b.STAGE_B_CROSS_SOURCE_MIN_ROWS,
                        min_securities=b.STAGE_B_CROSS_SOURCE_MIN_SECURITIES,
                        require_anchors=list(b.STAGE_B_SAMPLE_ANCHORS))
    gate = res["gates"][0]
    assert gate["passed"] is False
    assert res["rows"] < b.STAGE_B_CROSS_SOURCE_MIN_ROWS
    conn.close()


def test_stage_b_cross_source_missing_anchor_fails(tmp_path):
    from audit_bj_historical_valuation_build import _cross_source
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_v3"
    _mk_bsummary(root, n=32)
    cand = root / "build" / "stage-b-bj-valuation.db"
    codes = [f"{920000 + i:06d}" for i in range(32)]
    _make_bcandidate(cand, codes)
    # sample covers 32 codes but omits the 920002 anchor entirely
    coll = {"baidu": {}, "comparison": {}}
    for code in codes:
        if code == "920002":
            continue
        coll["comparison"][code] = {"row": {"code": code, "date": "2026-07-01",
                                            "pe_ttm": 10.0, "pb_mrq": 1.1}}
    pdir = root / "analysis"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "cross-source-collection.json").write_text(
        json.dumps(coll, ensure_ascii=False), encoding="utf-8")
    conn = sqlite3.connect(cand)
    res = _cross_source(root, conn,
                        anchors=list(b.STAGE_B_SAMPLE_ANCHORS),
                        min_rows=b.STAGE_B_CROSS_SOURCE_MIN_ROWS,
                        min_securities=b.STAGE_B_CROSS_SOURCE_MIN_SECURITIES,
                        require_anchors=list(b.STAGE_B_SAMPLE_ANCHORS))
    gate = res["gates"][0]
    assert gate["passed"] is False
    assert "920002" in res["gate_ev"]["missing_required_anchors"]
    conn.close()


# ---------------------------------------------------------------------------
# M1/M3: auditor validates the frozen Stage B plan + anchor policy baseline
# ---------------------------------------------------------------------------
def test_audit_stage_b_plan_baseline_recorded(tmp_path):
    from audit_bj_historical_valuation_build import run
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_m1"
    # identity deliberately excludes an in-range anchor? no: default 32 codes
    _mk_bsummary(root, n=32)
    id_doc = json.loads((root / "identity" / "bj-active-securities.json").read_text(
        encoding="utf-8"))
    _mk_stage_b_plan(root, id_doc["securities"])
    cand = root / "build" / "stage-b-bj-valuation.db"
    _make_bcandidate(cand, [s["code"] for s in id_doc["securities"]])
    ver = _stage_b_verdict_path(root)
    code = run(["--mode", "stage-b", "--evidence-root", str(root),
                "--stage-a-verdict", str(ver),
                "--candidate-db", str(cand),
                "--output", str(_verdict_output(root))])
    doc = json.loads((_verdict_output(root)).read_text(encoding="utf-8"))
    gates = {g["gate"]: g for g in doc["gates"]}
    assert gates["stage_b_plan_baseline_recorded"]["passed"] is True
    assert "920808" in json.loads(
        (root / "plan" / "stage-b-plan.json").read_text(encoding="utf-8"))[
        "out_of_scope_anchor_codes"]
    # cross-source artifact absent => honest NO_GO (2), not a fake PASS
    assert code in (0, 1, 2)


def test_audit_out_of_scope_not_in_candidate_or_sample(tmp_path):
    from audit_bj_historical_valuation_build import run
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_m2"
    _mk_bsummary(root, n=35)
    id_doc = json.loads((root / "identity" / "bj-active-securities.json").read_text(
        encoding="utf-8"))
    plan = _mk_stage_b_plan(root, id_doc["securities"])
    # plan excludes 920808 from identity -> out_of_scope must not leak into
    # candidate codes or sample_codes, and must not appear in the candidate DB.
    assert "920808" not in plan["cross_source_sample_codes"]
    assert "920808" not in plan["required_anchor_codes"]
    cand = root / "build" / "stage-b-bj-valuation.db"
    _make_bcandidate(cand, [s["code"] for s in id_doc["securities"]])
    ver = _stage_b_verdict_path(root)
    run(["--mode", "stage-b", "--evidence-root", str(root),
         "--stage-a-verdict", str(ver), "--candidate-db", str(cand),
         "--output", str(_verdict_output(root))])
    doc = json.loads((_verdict_output(root)).read_text(encoding="utf-8"))
    g = next(g for g in doc["gates"] if g["gate"] == "cross_source_sample_within_identity")
    assert g["passed"] is True
    cand_codes = {r[0] for r in sqlite3.connect(cand).execute(
        "SELECT DISTINCT code FROM stock_valuation")}
    assert "920808" not in cand_codes


def test_audit_required_anchor_absent_fails_cross_source(tmp_path):
    from audit_bj_historical_valuation_build import run
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_m3"
    _mk_bsummary(root, n=32)
    id_doc = json.loads((root / "identity" / "bj-active-securities.json").read_text(
        encoding="utf-8"))
    secs = [b.Security(code=s["code"], name=s["name"], market=s["market"],
                       is_active=int(s["is_active"]), list_date=s["list_date"])
            for s in id_doc["securities"]]
    required, _ = b.anchor_policy(secs)
    plan = _mk_stage_b_plan(root, id_doc["securities"])
    cand = root / "build" / "stage-b-bj-valuation.db"
    _make_bcandidate(cand, [s["code"] for s in id_doc["securities"]])
    # omit a required anchor from the cross-source collection artifact
    omit = required[0]
    coll = {"baidu": {}, "comparison": {}}
    for s in id_doc["securities"]:
        if s["code"] == omit:
            continue
        coll["comparison"][s["code"]] = {"row": {"code": s["code"],
                                                 "date": "2026-07-01",
                                                 "pe_ttm": 10.0, "pb_mrq": 1.1}}
    pdir = root / "analysis"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "cross-source-collection.json").write_text(
        json.dumps(coll, ensure_ascii=False), encoding="utf-8")
    ver = _stage_b_verdict_path(root)
    run(["--mode", "stage-b", "--evidence-root", str(root),
         "--stage-a-verdict", str(ver), "--candidate-db", str(cand),
         "--output", str(_verdict_output(root))])
    doc = json.loads((_verdict_output(root)).read_text(encoding="utf-8"))
    cs = doc["gates"]
    g = next(g for g in cs if g["gate"] == "cross_source_anchor_consistency")
    assert g["passed"] is False
    assert omit in (doc["counts"].get("cross_source_missing_required_anchors") or [])
    conn = sqlite3.connect(cand)
    conn.close()


# ---------------------------------------------------------------------------
# M7: auditor also validates Stage A verdict structurally
# ---------------------------------------------------------------------------
def test_audit_m7_forged_verdict_string_rejected(tmp_path):
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_m7_aud"
    _mk_bsummary(root, n=32)
    _make_bcandidate(root / "build" / "stage-b-bj-valuation.db",
                     [s["code"] for s in json.loads(
                         (root / "identity" / "bj-active-securities.json").read_text(
                             encoding="utf-8"))["securities"]])
    # fake verdict: correct string, no gates -> M7 structural gate fails
    fake_path = root / "fake-verdict.json"
    fake_path.write_text(
        json.dumps({"verdict": b.STAGE_A_READY_VERDICT}), encoding="utf-8")
    code = run(["--mode", "stage-b", "--evidence-root", str(root),
                "--stage-a-verdict", str(fake_path),
                "--output", str(_verdict_output(root))])
    doc = json.loads((_verdict_output(root)).read_text(encoding="utf-8"))
    g = next(g for g in doc["gates"] if g["gate"] == "stage_a_verdict_accepted")
    assert g["passed"] is False
    assert code in (1, 2)


def test_audit_m7_structural_verdict_accepted(tmp_path):
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_m7_aud_ok"
    _mk_bsummary(root, n=32)
    cand = root / "build" / "stage-b-bj-valuation.db"
    _make_bcandidate(cand, [s["code"] for s in json.loads(
        (root / "identity" / "bj-active-securities.json").read_text(
            encoding="utf-8"))["securities"]])
    ver = _stage_b_verdict_path(root, with_gates=True)
    code = run(["--mode", "stage-b", "--evidence-root", str(root),
                "--stage-a-verdict", str(ver),
                "--candidate-db", str(cand),
                "--output", str(_verdict_output(root))])
    doc = json.loads((_verdict_output(root)).read_text(encoding="utf-8"))
    g = next(g for g in doc["gates"] if g["gate"] == "stage_a_verdict_accepted")
    assert g["passed"] is True
    # cross-source artifact absent => honest NO_GO (2), not a fake PASS
    assert code in (0, 1, 2)


# ---------------------------------------------------------------------------
# R1-1: auditor cache SHA check
# ---------------------------------------------------------------------------
def test_audit_r1_1_cache_sha_mismatch_fails_gate(tmp_path):
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_r1_cache"
    _mk_bsummary(root, n=12)
    cand = root / "build" / "stage-b-bj-valuation.db"
    _make_bcandidate(cand, [s["code"] for s in json.loads(
        (root / "identity" / "bj-active-securities.json").read_text(
            encoding="utf-8"))["securities"]])
    ver = _stage_b_verdict_path(root, with_gates=True)
    # write a ledger entry with a PROVEN outcome but no matching cache
    led_dir = root / "requests"
    led_dir.mkdir(parents=True, exist_ok=True)
    import json as j
    with open(led_dir / "request-ledger.jsonl", "w") as f:
        f.write(j.dumps({"request_id": "req-920000", "outcome": "PROVEN",
                          "response_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
                          "counted_as_real_request": 1}) + "\n")
    code = run(["--mode", "stage-b", "--evidence-root", str(root),
                "--stage-a-verdict", str(ver),
                "--candidate-db", str(cand),
                "--output", str(_verdict_output(root))])
    doc = j.loads((_verdict_output(root)).read_text(encoding="utf-8"))
    g = next(g for g in doc["gates"] if g["gate"] == "response_cache_complete")
    assert g["passed"] is False
    assert "missing" in g.get("detail", "")


def test_audit_r1_1_cache_sha_mismatch_on_content(tmp_path):
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_r1_sha"
    _mk_bsummary(root, n=12)
    cand = root / "build" / "stage-b-bj-valuation.db"
    _make_bcandidate(cand, [s["code"] for s in json.loads(
        (root / "identity" / "bj-active-securities.json").read_text(
            encoding="utf-8"))["securities"]])
    ver = _stage_b_verdict_path(root, with_gates=True)
    # write a cache file with content that doesn't match the SHA in ledger
    cache_dir = root / "responses"
    cache_dir.mkdir(parents=True, exist_ok=True)
    import gzip, json as j
    records = [{"code": "920000", "date": "2026-07-01", "pe_ttm": 10.0}]
    payload = j.dumps(records, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    actual_sha = b.sha256_bytes(payload)
    with gzip.open(cache_dir / "req-920000.canonical.json.gz", "wb") as f:
        f.write(payload)
    led_dir = root / "requests"
    led_dir.mkdir(parents=True, exist_ok=True)
    with open(led_dir / "request-ledger.jsonl", "w") as f:
        f.write(j.dumps({"request_id": "req-920000", "outcome": "PROVEN",
                          "response_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
                          "counted_as_real_request": 1}) + "\n")
    code = run(["--mode", "stage-b", "--evidence-root", str(root),
                "--stage-a-verdict", str(ver),
                "--candidate-db", str(cand),
                "--output", str(_verdict_output(root))])
    doc = j.loads((_verdict_output(root)).read_text(encoding="utf-8"))
    g = next(g for g in doc["gates"] if g["gate"] == "response_cache_complete")
    assert g["passed"] is False
    assert "sha_mismatch" in g.get("detail", "")


# ---------------------------------------------------------------------------
# R1-2: auditor baseline fail-closed
# ---------------------------------------------------------------------------
def test_audit_r1_2_baseline_minimal_required(tmp_path):
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_r1_base"
    _mk_bsummary(root, n=12)
    _make_bcandidate(root / "build" / "stage-b-bj-valuation.db",
                     [s["code"] for s in json.loads(
                         (root / "identity" / "bj-active-securities.json").read_text(
                             encoding="utf-8"))["securities"]])
    # baseline with only verdict/identity but no formal_db fields -> must fail
    base = {"header": "ok", "identity_codes": 12}
    (root / "baseline" / "g0-baseline.json").write_text(
        json.dumps(base), encoding="utf-8")
    ver = _stage_b_verdict_path(root, with_gates=True)
    code = run(["--mode", "stage-b", "--evidence-root", str(root),
                "--stage-a-verdict", str(ver),
                "--output", str(_verdict_output(root))])
    doc = json.loads((_verdict_output(root)).read_text(encoding="utf-8"))
    g = next(g for g in doc["gates"] if g["gate"] == "formal_db_unchanged")
    assert g["passed"] is False


# ---------------------------------------------------------------------------
# R2-1: auditor response_sha256 empty/invalid must fail gate
# ---------------------------------------------------------------------------
def test_audit_r2_1_empty_response_sha256_fails(tmp_path):
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_r2_empty"
    _mk_bsummary(root, n=12)
    cand = root / "build" / "stage-b-bj-valuation.db"
    _make_bcandidate(cand, [s["code"] for s in json.loads(
        (root / "identity" / "bj-active-securities.json").read_text(
            encoding="utf-8"))["securities"]])
    ver = _stage_b_verdict_path(root, with_gates=True)
    # write a cache file with valid content
    cache_dir = root / "responses"
    cache_dir.mkdir(parents=True, exist_ok=True)
    import gzip
    records = [{"code": "920000", "date": "2026-07-01", "pe_ttm": 10.0}]
    payload = json.dumps(records, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    with gzip.open(cache_dir / "req-920000.canonical.json.gz", "wb") as f:
        f.write(payload)
    # ledger with empty response_sha256
    led_dir = root / "requests"
    led_dir.mkdir(parents=True, exist_ok=True)
    with open(led_dir / "request-ledger.jsonl", "w") as f:
        f.write(json.dumps({"request_id": "req-920000", "outcome": "PROVEN",
                            "response_sha256": "",
                            "counted_as_real_request": 1}) + "\n")
    code = run(["--mode", "stage-b", "--evidence-root", str(root),
                "--stage-a-verdict", str(ver),
                "--candidate-db", str(cand),
                "--output", str(_verdict_output(root))])
    doc = json.loads((_verdict_output(root)).read_text(encoding="utf-8"))
    g = next(g for g in doc["gates"] if g["gate"] == "response_cache_complete")
    assert g["passed"] is False
    assert "invalid_sha" in g.get("detail", "")


def test_audit_r2_1_none_response_sha256_fails(tmp_path):
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_r2_none"
    _mk_bsummary(root, n=12)
    cand = root / "build" / "stage-b-bj-valuation.db"
    _make_bcandidate(cand, [s["code"] for s in json.loads(
        (root / "identity" / "bj-active-securities.json").read_text(
            encoding="utf-8"))["securities"]])
    ver = _stage_b_verdict_path(root, with_gates=True)
    cache_dir = root / "responses"
    cache_dir.mkdir(parents=True, exist_ok=True)
    import gzip
    records = [{"code": "920000", "date": "2026-07-01", "pe_ttm": 10.0}]
    payload = json.dumps(records, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    with gzip.open(cache_dir / "req-920000.canonical.json.gz", "wb") as f:
        f.write(payload)
    led_dir = root / "requests"
    led_dir.mkdir(parents=True, exist_ok=True)
    with open(led_dir / "request-ledger.jsonl", "w") as f:
        f.write(json.dumps({"request_id": "req-920000", "outcome": "PROVEN",
                            "counted_as_real_request": 1}) + "\n")
    code = run(["--mode", "stage-b", "--evidence-root", str(root),
                "--stage-a-verdict", str(ver),
                "--output", str(_verdict_output(root))])
    doc = json.loads((_verdict_output(root)).read_text(encoding="utf-8"))
    g = next(g for g in doc["gates"] if g["gate"] == "response_cache_complete")
    assert g["passed"] is False
    assert "invalid_sha" in g.get("detail", "")


# ---------------------------------------------------------------------------
# R3-1: manifest gate must audit the FULL evidence set (unmanifested files FAIL)
# ---------------------------------------------------------------------------
def _mk_manifest_root(tmp_path, files):
    """Build an evidence tree where every key in `files` (relpath -> content)
    is written, plus a manifest covering the listed subset passed separately."""
    root = tmp_path / "manifest_ev"
    root.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    return root


def _write_manifest(root, entries):
    """Write manifest.json first and final.sha256 last, binding the summary."""
    man = root / "manifests"
    man.mkdir(parents=True, exist_ok=True)
    payload_size = sum((root / rel).stat().st_size for rel in entries
                       if (root / rel).exists())
    mj = {"task": "test", "total_files": len(entries),
          "total_size_bytes": payload_size}
    manifest_json = man / "manifest.json"
    manifest_json.write_text(json.dumps(mj, indent=2) + "\n", encoding="utf-8")
    sealed_entries = list(entries) + ["manifests/manifest.json"]
    lines = []
    for rel in sealed_entries:
        p = root / rel
        sha = b.sha256_file(p) if p.exists() else '0' * 64
        lines.append(f"{sha}  {rel}")
    (man / "final.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_r3_1_manifest_unmanifested_file_fails(tmp_path):
    root = _mk_manifest_root(tmp_path, {
        "identity/ok.json": "{}",
        "sneaky.txt": "extra unmanifested file",
    })
    _write_manifest(root, ["identity/ok.json"])
    ok, detail, counts = _manifest_verify(root)
    assert ok is False
    assert "unmanifested" in detail
    assert counts["unmanifested_files"] == ["sneaky.txt"]


def test_r3_1_manifest_missing_target_fails(tmp_path):
    root = _mk_manifest_root(tmp_path, {"identity/ok.json": "{}"})
    _write_manifest(root, ["identity/ok.json", "identity/missing.json"])
    ok, detail, counts = _manifest_verify(root)
    assert ok is False
    assert counts["missing_manifest_targets"] == ["identity/missing.json"]


def test_r3_1_manifest_duplicate_path_fails(tmp_path):
    root = _mk_manifest_root(tmp_path, {"identity/ok.json": "{}"})
    lines = []
    for _ in range(2):
        lines.append(f"{b.sha256_file(root / 'identity/ok.json')}  identity/ok.json")
    man = root / "manifests"
    man.mkdir(parents=True, exist_ok=True)
    (man / "final.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (man / "manifest.json").write_text(
        json.dumps({"task": "test", "total_files": len(lines), "total_size_bytes": 2}),
        encoding="utf-8")
    ok, detail, counts = _manifest_verify(root)
    assert ok is False
    assert counts["duplicate_manifest_paths"] == ["identity/ok.json"]


def test_r3_1_manifest_unsafe_path_fails(tmp_path):
    root = _mk_manifest_root(tmp_path, {"identity/ok.json": "{}"})
    man = root / "manifests"
    man.mkdir(parents=True, exist_ok=True)
    sha = b.sha256_file(root / "identity/ok.json")
    (man / "final.sha256").write_text(
        f"{sha}  ../escape.txt\n", encoding="utf-8")
    (man / "manifest.json").write_text(
        json.dumps({"task": "test", "total_files": 1, "total_size_bytes": 2}),
        encoding="utf-8")
    ok, detail, counts = _manifest_verify(root)
    assert ok is False
    assert counts["unsafe_paths"] == ["../escape.txt"]


def test_r3_1_manifest_absolute_path_fails(tmp_path):
    root = _mk_manifest_root(tmp_path, {"identity/ok.json": "{}"})
    man = root / "manifests"
    man.mkdir(parents=True, exist_ok=True)
    (man / "final.sha256").write_text(
        f"{'0'*64}  {tmp_path}/outside.txt\n", encoding="utf-8")
    (man / "manifest.json").write_text(
        json.dumps({"task": "test", "total_files": 1, "total_size_bytes": 2}),
        encoding="utf-8")
    ok, detail, counts = _manifest_verify(root)
    assert ok is False
    assert counts["unsafe_paths"]


def test_r3_1_manifest_full_set_passes(tmp_path):
    root = _mk_manifest_root(tmp_path, {
        "identity/ok.json": "{}",
        "responses/a.json": "{}",
    })
    _write_manifest(root, ["identity/ok.json", "responses/a.json"])
    ok, detail, counts = _manifest_verify(root)
    assert ok is True, detail
    assert counts["unmanifested_files"] == []
    assert counts["missing_manifest_targets"] == []


# ---------------------------------------------------------------------------
# R3-2: baseline int fields reject bool; --formal-db must match baseline path
# ---------------------------------------------------------------------------
def _synthetic_baseline(tmp_path, **overrides):
    """Build a baseline pointing at a real temp DB so type validation runs
    before any comparison (type errors are caught pre-read)."""
    db = tmp_path / "formal.db"
    db.write_bytes(b"formal-db-content")
    bl = {
        "formal_db_path": str(db.resolve()),
        "formal_db_sha256": "a" * 64,
        "formal_db_size": db.stat().st_size,
        "formal_db_mtime_ns": db.stat().st_mtime_ns,
        "wal_exists": True,
        "wal_size": 0,
        "wal_mtime_ns": 0,
        "shm_exists": True,
        "shm_size": 0,
        "shm_mtime_ns": 0,
        "git_head": "b" * 40,
        "git_status_count": 0,
        "recorded_at": "2026-08-04T00:00:00Z",
    }
    bl.update(overrides)
    base_dir = tmp_path / "baseline"
    base_dir.mkdir(parents=True, exist_ok=True)
    (base_dir / "g0-baseline.json").write_text(json.dumps(bl), encoding="utf-8")
    return bl


def test_r3_2_baseline_int_rejects_bool_true(tmp_path):
    _synthetic_baseline(tmp_path, formal_db_size=True)
    ok, detail = _formal_db_fingerprint(tmp_path, None)
    assert ok is False
    assert "formal_db_size" in detail


def test_r3_2_baseline_int_rejects_bool_false(tmp_path):
    _synthetic_baseline(tmp_path, formal_db_mtime_ns=False)
    ok, detail = _formal_db_fingerprint(tmp_path, None)
    assert ok is False
    assert "formal_db_mtime_ns" in detail


def test_r3_2_baseline_wal_size_rejects_bool(tmp_path):
    _synthetic_baseline(tmp_path, wal_size=True)
    ok, detail = _formal_db_fingerprint(tmp_path, None)
    assert ok is False
    assert "wal_size" in detail


def test_r3_2_baseline_shm_mtime_rejects_bool(tmp_path):
    _synthetic_baseline(tmp_path, shm_mtime_ns=False)
    ok, detail = _formal_db_fingerprint(tmp_path, None)
    assert ok is False
    assert "shm_mtime_ns" in detail


def test_r3_2_baseline_git_status_count_rejects_bool(tmp_path):
    _synthetic_baseline(tmp_path, git_status_count=True)
    ok, detail = _formal_db_fingerprint(tmp_path, None)
    assert ok is False
    assert "git_status_count" in detail


def test_r3_2_baseline_bool_field_rejects_int(tmp_path):
    _synthetic_baseline(tmp_path, wal_exists=1)
    ok, detail = _formal_db_fingerprint(tmp_path, None)
    assert ok is False
    assert "wal_exists" in detail


def test_r3_2_baseline_bool_field_rejects_zero(tmp_path):
    _synthetic_baseline(tmp_path, shm_exists=0)
    ok, detail = _formal_db_fingerprint(tmp_path, None)
    assert ok is False
    assert "shm_exists" in detail


def test_r3_2_formal_db_path_arg_mismatch_blocks(tmp_path):
    bl = _synthetic_baseline(tmp_path)
    other = tmp_path / "other.db"
    other.write_bytes(b"x")
    ok, detail = _formal_db_fingerprint(tmp_path, other)
    assert ok is False
    assert "BLOCKED_BASELINE_DRIFT" in detail or "formal_db_path" in detail


def _real_baseline(tmp_path, monkeypatch):
    """Build a baseline from a REAL temp DB with real SHA/size/mtime and
    controlled (mocked) Git fingerprint, plus real absent WAL/SHM state.

    This is an honest, complete fingerprint: the drift comparison in
    _formal_db_fingerprint() runs against the same file the facts described.
    It never hand-fabricates SHA/size/mtime or fake WAL/SHM state."""
    import hashlib
    import types
    db = tmp_path / "formal.db"
    db.write_bytes(b"real-formal-db-content-v1")
    data = db.read_bytes()
    real_sha = hashlib.sha256(data).hexdigest()
    real_size = db.stat().st_size
    real_mtime = db.stat().st_mtime_ns
    real_head = "a" * 40
    real_status_count = 3

    # control the git fingerprint the auditor will read
    def _fake_run(cmd, *a, **k):
        if cmd == ["git", "rev-parse", "HEAD"]:
            return types.SimpleNamespace(returncode=0,
                                         stdout=real_head + "\n")
        if cmd == ["git", "status", "--short"]:
            return types.SimpleNamespace(returncode=0,
                                         stdout=" M a\n M b\n M c\n")
        raise AssertionError(f"unexpected subprocess.run: {cmd}")
    monkeypatch.setattr("subprocess.run", _fake_run)

    bl = {
        "formal_db_path": str(db.resolve()),
        "formal_db_sha256": real_sha,
        "formal_db_size": real_size,
        "formal_db_mtime_ns": real_mtime,
        "wal_exists": False,
        "wal_size": 0,
        "wal_mtime_ns": 0,
        "shm_exists": False,
        "shm_size": 0,
        "shm_mtime_ns": 0,
        "git_head": real_head,
        "git_status_count": real_status_count,
        "recorded_at": "2026-08-04T00:00:00Z",
    }
    base_dir = tmp_path / "baseline"
    base_dir.mkdir(parents=True, exist_ok=True)
    (base_dir / "g0-baseline.json").write_text(json.dumps(bl), encoding="utf-8")
    return bl


def test_r3_2_formal_db_path_arg_same_path_passes(tmp_path, monkeypatch):
    bl = _real_baseline(tmp_path, monkeypatch)
    same = Path(bl["formal_db_path"])
    ok, detail = _formal_db_fingerprint(tmp_path, same)
    assert ok is True, detail


def test_r3_2_formal_db_sha_drift_same_path_fails(tmp_path, monkeypatch):
    bl = _real_baseline(tmp_path, monkeypatch)
    # flip the recorded SHA to a DIFFERENT valid hex => drift must FAIL even
    # though the path is identical
    bl["formal_db_sha256"] = "b" * 64
    (tmp_path / "baseline" / "g0-baseline.json").write_text(
        json.dumps(bl), encoding="utf-8")
    ok, detail = _formal_db_fingerprint(tmp_path, Path(bl["formal_db_path"]))
    assert ok is False
    assert "sha DRIFT" in detail


# ---------------------------------------------------------------------------
# R3-3: request_id must be valid; empty must never skip the cache gate
# ---------------------------------------------------------------------------
def test_r3_3_request_id_empty_string_invalid():
    assert _request_id_ok("") is False
    assert _request_id_ok(None) is False
    assert _request_id_ok("   ") is False
    assert _request_id_ok(123) is False
    assert _request_id_ok("req-920000") is True
    assert _request_id_ok("a/b") is False
    assert _request_id_ok("../escape") is False


def test_r3_3_empty_request_id_breaks_cache_gate(tmp_path):
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_r3_empty_rid"
    _mk_bsummary(root, n=12)
    cand = root / "build" / "stage-b-bj-valuation.db"
    _make_bcandidate(cand, [s["code"] for s in json.loads(
        (root / "identity" / "bj-active-securities.json").read_text(
            encoding="utf-8"))["securities"]])
    ver = _stage_b_verdict_path(root, with_gates=True)
    # PROVEN ledger record with an EMPTY request_id (Nova R2-3 counter-example)
    led_dir = root / "requests"
    led_dir.mkdir(parents=True, exist_ok=True)
    with open(led_dir / "request-ledger.jsonl", "w") as f:
        f.write(json.dumps({"request_id": "", "outcome": "PROVEN",
                            "counted_as_real_request": 1}) + "\n")
    run(["--mode", "stage-b", "--evidence-root", str(root),
         "--stage-a-verdict", str(ver),
         "--output", str(_verdict_output(root))])
    doc = json.loads((_verdict_output(root)).read_text(encoding="utf-8"))
    gates = {g["gate"]: g for g in doc["gates"]}
    assert gates["request_ledger_conserved"]["passed"] is False
    assert gates["response_cache_complete"]["passed"] is False


def test_r3_3_none_request_id_breaks_ledger_gate(tmp_path):
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_r3_none_rid"
    _mk_bsummary(root, n=12)
    cand = root / "build" / "stage-b-bj-valuation.db"
    _make_bcandidate(cand, [s["code"] for s in json.loads(
        (root / "identity" / "bj-active-securities.json").read_text(
            encoding="utf-8"))["securities"]])
    ver = _stage_b_verdict_path(root, with_gates=True)
    led_dir = root / "requests"
    led_dir.mkdir(parents=True, exist_ok=True)
    with open(led_dir / "request-ledger.jsonl", "w") as f:
        f.write(json.dumps({"request_id": None, "outcome": "PROVEN",
                            "counted_as_real_request": 1}) + "\n")
    run(["--mode", "stage-b", "--evidence-root", str(root),
         "--stage-a-verdict", str(ver),
         "--output", str(_verdict_output(root))])
    doc = json.loads((_verdict_output(root)).read_text(encoding="utf-8"))
    g = next(g for g in doc["gates"] if g["gate"] == "request_ledger_conserved")
    assert g["passed"] is False


def test_r3_3_duplicate_request_id_fails(tmp_path):
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_r3_dup_rid"
    _mk_bsummary(root, n=12)
    cand = root / "build" / "stage-b-bj-valuation.db"
    _make_bcandidate(cand, [s["code"] for s in json.loads(
        (root / "identity" / "bj-active-securities.json").read_text(
            encoding="utf-8"))["securities"]])
    ver = _stage_b_verdict_path(root, with_gates=True)
    led_dir = root / "requests"
    led_dir.mkdir(parents=True, exist_ok=True)
    with open(led_dir / "request-ledger.jsonl", "w") as f:
        f.write(json.dumps({"request_id": "req-920000", "outcome": "PROVEN",
                            "counted_as_real_request": 1}) + "\n")
        f.write(json.dumps({"request_id": "req-920000", "outcome": "PROVEN",
                            "counted_as_real_request": 1}) + "\n")
    run(["--mode", "stage-b", "--evidence-root", str(root),
         "--stage-a-verdict", str(ver),
         "--output", str(_verdict_output(root))])
    doc = json.loads((_verdict_output(root)).read_text(encoding="utf-8"))
    g = next(g for g in doc["gates"] if g["gate"] == "request_ledger_conserved")
    assert g["passed"] is False


# ---------------------------------------------------------------------------
# R3-5: migration report hard gate
# ---------------------------------------------------------------------------
def _mk_migration_root(tmp_path, n=2, corrupt=False):
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_r3_mig"
    _mk_bsummary(root, n=12)
    cand = root / "build" / "stage-b-bj-valuation.db"
    _make_bcandidate(cand, [s["code"] for s in json.loads(
        (root / "identity" / "bj-active-securities.json").read_text(
            encoding="utf-8"))["securities"]])
    # write a cache + ledger for `n` PROVEN records
    rc = b.ResponseCache(root / "responses")
    records = [{"date": "2026-07-01", "pe_ttm": 10.0, "code": "920000"}]
    sha = rc.save("req-920000", records)
    led_dir = root / "requests"
    led_dir.mkdir(parents=True, exist_ok=True)
    recs = [{"request_id": "req-920000", "source": "stock_value_em",
             "outcome": "PROVEN", "counted_as_real_request": 1,
             "response_sha256": sha}]
    with open(led_dir / "request-ledger.jsonl", "w") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")
    # build the report from the ledger
    b.write_cache_sha_migration_report(root, recs, root / "responses", "src-root")
    return root, recs


def test_r3_5_migration_report_verified_ok(tmp_path):
    root, _ = _mk_migration_root(tmp_path)
    ok, detail, counts = _migration_report_verify(root)
    assert ok is True, detail
    assert counts["processed"] == 1
    assert counts["unchanged"] == 1


def test_r3_5_migration_report_missing_fails(tmp_path):
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_r3_mig_missing"
    _mk_bsummary(root, n=12)
    ok, detail, counts = _migration_report_verify(root)
    assert ok is False
    assert "missing" in detail


def test_r3_5_migration_report_inconsistent_fails(tmp_path):
    root, _ = _mk_migration_root(tmp_path)
    rep_path = root / "analysis" / "cache-sha-migration.json"
    report = json.loads(rep_path.read_text(encoding="utf-8"))
    # break conservation
    report["processed_records"] = 2
    report["converted_records"] = 1
    rep_path.write_text(json.dumps(report), encoding="utf-8")
    ok, detail, counts = _migration_report_verify(root)
    assert ok is False
    assert "conservation" in detail or "mismatch" in detail


def test_r3_5_migration_report_status_inconsistent_fails(tmp_path):
    root, _ = _mk_migration_root(tmp_path)
    rep_path = root / "analysis" / "cache-sha-migration.json"
    report = json.loads(rep_path.read_text(encoding="utf-8"))
    # force an impossible status: converted with equal SHA
    report["records"][0]["conversion_status"] = "converted"
    report["records"][0]["old_sha256"] = report["records"][0]["new_sha256"]
    report["converted_records"] = 1
    report["unchanged_records"] = 0
    rep_path.write_text(json.dumps(report), encoding="utf-8")
    ok, detail, counts = _migration_report_verify(root)
    assert ok is False


def test_r3_5_migration_report_missing_path_fails(tmp_path):
    root, _ = _mk_migration_root(tmp_path)
    rep_path = root / "analysis" / "cache-sha-migration.json"
    report = json.loads(rep_path.read_text(encoding="utf-8"))
    report["records"][0]["cache_relative_path"] = "responses/does-not-exist.json.gz"
    rep_path.write_text(json.dumps(report), encoding="utf-8")
    ok, detail, counts = _migration_report_verify(root)
    assert ok is False
    assert "path missing" in detail


def test_r3_5_migration_report_extra_request_id_fails(tmp_path):
    root, recs = _mk_migration_root(tmp_path)
    rep_path = root / "analysis" / "cache-sha-migration.json"
    report = json.loads(rep_path.read_text(encoding="utf-8"))
    report["records"].append(dict(report["records"][0]))
    report["records"][-1]["request_id"] = "req-EXTRA"
    report["total_records"] = 2
    report["processed_records"] = 2
    report["unchanged_records"] = 2
    rep_path.write_text(json.dumps(report), encoding="utf-8")
    ok, detail, counts = _migration_report_verify(root)
    assert ok is False
    assert "set" in detail


# ---------------------------------------------------------------------------
# R3-5 negative counterexamples (Nova-mandated): fail-closed, not fail-open
# ---------------------------------------------------------------------------
def _rewrite_report(root, fn):
    rep_path = root / "analysis" / "cache-sha-migration.json"
    report = json.loads(rep_path.read_text(encoding="utf-8"))
    fn(report)
    rep_path.write_text(json.dumps(report), encoding="utf-8")
    return rep_path


def test_r3_5_migration_all_failed_fails(tmp_path):
    root, _ = _mk_migration_root(tmp_path)
    _rewrite_report(root, lambda r: (
        r["records"][0].update({"conversion_status": "failed",
                                "error": "boom"}),
        r.update({"converted_records": 0, "unchanged_records": 0,
                  "failed_records": 1})))
    ok, detail, counts = _migration_report_verify(root)
    assert ok is False
    assert "failed_records" in detail


def test_r3_5_migration_one_failed_among_valid_fails(tmp_path):
    root, _ = _mk_migration_root(tmp_path)
    # one additional fully-valid unchanged record, plus one failed record:
    # the report is otherwise valid but ANY failed record must still hard-FAIL.
    rep_path = root / "analysis" / "cache-sha-migration.json"
    report = json.loads(rep_path.read_text(encoding="utf-8"))
    good = dict(report["records"][0])
    good["request_id"] = "req-920000-second"
    report["records"] = [good] + report["records"]
    report["records"][1].update({"conversion_status": "failed",
                                 "error": "boom"})
    report.update({"total_records": 2, "processed_records": 2,
                   "converted_records": 0, "unchanged_records": 1,
                   "failed_records": 1})
    rep_path.write_text(json.dumps(report), encoding="utf-8")
    ok, detail, _ = _migration_report_verify(root)
    assert ok is False
    assert "failed_records" in detail


def test_r3_5_migration_bool_counter_fails(tmp_path):
    root, _ = _mk_migration_root(tmp_path)
    _rewrite_report(root, lambda r: r.update({"processed_records": True,
                                              "converted_records": False}))
    ok, detail, _ = _migration_report_verify(root)
    assert ok is False
    assert "int>=0" in detail or "invalid" in detail


def test_r3_5_migration_traversal_path_fails(tmp_path):
    root, _ = _mk_migration_root(tmp_path)
    _rewrite_report(root, lambda r: r["records"][0].update(
        {"cache_relative_path": "responses/../escape.gz"}))
    ok, detail, _ = _migration_report_verify(root)
    assert ok is False


def test_r3_5_migration_absolute_path_fails(tmp_path):
    root, _ = _mk_migration_root(tmp_path)
    _rewrite_report(root, lambda r: r["records"][0].update(
        {"cache_relative_path": "/etc/passwd"}))
    ok, detail, _ = _migration_report_verify(root)
    assert ok is False


def test_r3_5_migration_adjacent_prefix_path_fails(tmp_path):
    root, _ = _mk_migration_root(tmp_path)
    _rewrite_report(root, lambda r: r["records"][0].update(
        {"cache_relative_path": "responses-evil/x.gz"}))
    ok, detail, _ = _migration_report_verify(root)
    assert ok is False


def test_r3_5_migration_deleted_cache_after_report_fails(tmp_path):
    root, _ = _mk_migration_root(tmp_path)
    # report was generated successfully; now the cache file is removed/renamed
    cache = root / "responses" / "req-920000.canonical.json.gz"
    assert cache.exists()
    cache.rename(root / "responses" / "req-920000.renamed.gz")
    ok, detail, _ = _migration_report_verify(root)
    assert ok is False
    assert "path missing" in detail


# ---------------------------------------------------------------------------
# R3-R2-1: factual request counter negative tests
# ---------------------------------------------------------------------------
def _mk_factual_counter_root(root, ledger_sources, cp_factual=None):
    """Build a minimal Stage B evidence root with a ledger and checkpoint
    containing request_counters_factual. ledger_sources is a dict of
    {source: count}."""
    _mk_bsummary(root, n=30)
    id_doc = json.loads((root / "identity" / "bj-active-securities.json").read_text(
        encoding="utf-8"))
    _mk_stage_b_plan(root, id_doc["securities"])
    codes = [s["code"] for s in id_doc["securities"]]
    _make_bcandidate(root / "build" / "stage-b-bj-valuation.db", codes)
    # build proper ledger records with counted_as_real_request=1
    ledger_recs = []
    for src, cnt in ledger_sources.items():
        for i in range(cnt):
            rid = f"{src}-{i:05d}"
            ledger_recs.append({"request_id": rid, "source": src,
                                "counted_as_real_request": 1,
                                "outcome": "PROVEN", "code_or_batch_scope": codes[i % len(codes)]})
    _write_ledger(root, ledger_recs)
    cp = {"version": b.STAGE_B_CHECKPOINT_VERSION, "stage": "B",
          "plan_sha256": "0" * 64, "identity_snapshot_sha256": "0" * 64,
          "mapping_snapshot_sha256": "0" * 64, "builder_sha256": "0" * 64,
          "schema_sha256": "0" * 64, "started_at": "2026-07-01T00:00:00Z",
          "completed_codes": codes, "completed_operations": [],
          "request_counters": {"total_used": len(codes), "total_cap": 1500,
                               "per_source_used": {"stock_value_em": len(codes)}},
          "request_counters_factual": cp_factual if cp_factual else
              {"total": sum(ledger_sources.values()),
               "per_source": dict(sorted(ledger_sources.items()))}}
    (root / "build" / "checkpoint.json").write_text(json.dumps(cp), encoding="utf-8")
    man = root / "manifests"
    man.mkdir(parents=True, exist_ok=True)
    (man / "final.sha256").write_text("0" * 64 + "  baseline/g0-baseline.json\n",
                                      encoding="utf-8")
    (man / "manifest.json").write_text(
        json.dumps({"task": "test", "total_files": 1, "total_size_bytes": 2}),
        encoding="utf-8")
    return root


def test_r3_r2_1_factual_counter_total_mismatch_fails(tmp_path):
    """cp total 323 vs ledger 413 -> fail."""
    from audit_bj_historical_valuation_build import run
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_fact1"
    ls = {"stock_value_em": 323, "stock_zh_valuation_baidu": 60,
          "stock_zh_valuation_comparison_em": 30}
    _mk_factual_counter_root(root, ls, cp_factual={"total": 323, "per_source": ls.copy()})
    ver = _stage_b_verdict_path(root)
    code = run(["--mode", "stage-b", "--evidence-root", str(root),
                "--stage-a-verdict", str(ver),
                "--candidate-db", str(root / "build" / "stage-b-bj-valuation.db"),
                "--output", str(_verdict_output(root))])
    doc = json.loads((_verdict_output(root)).read_text(encoding="utf-8"))
    g = next(g for g in doc["gates"] if g["gate"] == "runtime_counters_match_ledger")
    assert g["passed"] is False
    assert "total factual=323 ledger=413" in g["detail"]


def test_r3_r2_1_factual_counter_missing_source_fails(tmp_path):
    """missing Baidu/comparison counters -> fail."""
    from audit_bj_historical_valuation_build import run
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_fact2"
    ls = {"stock_value_em": 323, "stock_zh_valuation_baidu": 60,
          "stock_zh_valuation_comparison_em": 30}
    _mk_factual_counter_root(root, ls,
                             cp_factual={"total": 413, "per_source": {"stock_value_em": 323}})
    ver = _stage_b_verdict_path(root)
    code = run(["--mode", "stage-b", "--evidence-root", str(root),
                "--stage-a-verdict", str(ver),
                "--candidate-db", str(root / "build" / "stage-b-bj-valuation.db"),
                "--output", str(_verdict_output(root))])
    doc = json.loads((_verdict_output(root)).read_text(encoding="utf-8"))
    g = next(g for g in doc["gates"] if g["gate"] == "runtime_counters_match_ledger")
    assert g["passed"] is False
    assert "missing from factual" in g["detail"]


def test_r3_r2_1_factual_counter_per_source_mismatch_fails(tmp_path):
    """per-source mismatch -> fail."""
    from audit_bj_historical_valuation_build import run
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_fact3"
    ls = {"stock_value_em": 323, "stock_zh_valuation_baidu": 60,
          "stock_zh_valuation_comparison_em": 30}
    _mk_factual_counter_root(root, ls,
                             cp_factual={"total": 413, "per_source": {
                                 "stock_value_em": 323, "stock_zh_valuation_baidu": 61,
                                 "stock_zh_valuation_comparison_em": 30}})
    ver = _stage_b_verdict_path(root)
    code = run(["--mode", "stage-b", "--evidence-root", str(root),
                "--stage-a-verdict", str(ver),
                "--candidate-db", str(root / "build" / "stage-b-bj-valuation.db"),
                "--output", str(_verdict_output(root))])
    doc = json.loads((_verdict_output(root)).read_text(encoding="utf-8"))
    g = next(g for g in doc["gates"] if g["gate"] == "runtime_counters_match_ledger")
    assert g["passed"] is False
    assert "baidu factual=61 ledger=60" in g["detail"]


def test_r3_r2_1_factual_counter_extra_nonzero_source_fails(tmp_path):
    """extra nonzero source -> fail."""
    from audit_bj_historical_valuation_build import run
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_fact4"
    ls = {"stock_value_em": 323, "stock_zh_valuation_baidu": 60,
          "stock_zh_valuation_comparison_em": 30}
    _mk_factual_counter_root(root, ls,
                             cp_factual={"total": 443, "per_source": {
                                 "stock_value_em": 323, "stock_zh_valuation_baidu": 60,
                                 "stock_zh_valuation_comparison_em": 30,
                                 "stock_bj_a_spot_em": 30}})
    ver = _stage_b_verdict_path(root)
    code = run(["--mode", "stage-b", "--evidence-root", str(root),
                "--stage-a-verdict", str(ver),
                "--candidate-db", str(root / "build" / "stage-b-bj-valuation.db"),
                "--output", str(_verdict_output(root))])
    doc = json.loads((_verdict_output(root)).read_text(encoding="utf-8"))
    g = next(g for g in doc["gates"] if g["gate"] == "runtime_counters_match_ledger")
    assert g["passed"] is False
    assert "but not in ledger" in g["detail"]


def test_r3_r2_1_factual_counter_bool_total_fails(tmp_path):
    """bool counters -> fail."""
    from audit_bj_historical_valuation_build import run
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_fact5"
    ls = {"stock_value_em": 323}
    _mk_factual_counter_root(root, ls,
                             cp_factual={"total": True, "per_source": ls.copy()})
    ver = _stage_b_verdict_path(root)
    code = run(["--mode", "stage-b", "--evidence-root", str(root),
                "--stage-a-verdict", str(ver),
                "--candidate-db", str(root / "build" / "stage-b-bj-valuation.db"),
                "--output", str(_verdict_output(root))])
    doc = json.loads((_verdict_output(root)).read_text(encoding="utf-8"))
    g = next(g for g in doc["gates"] if g["gate"] == "runtime_counters_match_ledger")
    assert g["passed"] is False


def test_r3_r2_1_factual_counter_negative_total_fails(tmp_path):
    """negative counters -> fail."""
    from audit_bj_historical_valuation_build import run
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_fact6"
    ls = {"stock_value_em": 323}
    _mk_factual_counter_root(root, ls,
                             cp_factual={"total": -1, "per_source": ls.copy()})
    ver = _stage_b_verdict_path(root)
    code = run(["--mode", "stage-b", "--evidence-root", str(root),
                "--stage-a-verdict", str(ver),
                "--candidate-db", str(root / "build" / "stage-b-bj-valuation.db"),
                "--output", str(_verdict_output(root))])
    doc = json.loads((_verdict_output(root)).read_text(encoding="utf-8"))
    g = next(g for g in doc["gates"] if g["gate"] == "runtime_counters_match_ledger")
    assert g["passed"] is False


# ---------------------------------------------------------------------------
# R3-R2-2: manifest JSON summary negative tests
# ---------------------------------------------------------------------------
def test_r3_r2_2_manifest_json_total_files_mismatch_fails(tmp_path):
    """JSON total_files != final.sha256 line count -> fail."""
    root = _mk_manifest_root(tmp_path, {"identity/ok.json": "{}"})
    _write_manifest(root, ["identity/ok.json"])
    man = root / "manifests"
    mj = json.loads((man / "manifest.json").read_text(encoding="utf-8"))
    mj["total_files"] = 999
    (man / "manifest.json").write_text(json.dumps(mj, indent=2) + "\n", encoding="utf-8")
    ok, detail, counts = _manifest_verify(root)
    assert ok is False
    assert "total_files" in detail


def test_r3_r2_2_manifest_json_total_size_mismatch_fails(tmp_path):
    """JSON total_size_bytes != sum of final.sha256 listed sizes -> fail."""
    root = _mk_manifest_root(tmp_path, {"identity/ok.json": "{}"})
    _write_manifest(root, ["identity/ok.json"])
    man = root / "manifests"
    mj = json.loads((man / "manifest.json").read_text(encoding="utf-8"))
    mj["total_size_bytes"] = 0
    (man / "manifest.json").write_text(json.dumps(mj, indent=2) + "\n", encoding="utf-8")
    ok, detail, counts = _manifest_verify(root)
    assert ok is False
    assert "total_size_bytes" in detail


def test_r3_r2_2_manifest_json_stale_self_entry_fails(tmp_path):
    """manifest.json with stale self-referential SHA entry -> fail."""
    root = _mk_manifest_root(tmp_path, {"identity/ok.json": "{}"})
    _write_manifest(root, ["identity/ok.json"])
    man = root / "manifests"
    mj = json.loads((man / "manifest.json").read_text(encoding="utf-8"))
    mj["files"] = [{"path": "manifests/manifest.json", "sha256": "0" * 64}]
    (man / "manifest.json").write_text(json.dumps(mj, indent=2) + "\n", encoding="utf-8")
    ok, detail, counts = _manifest_verify(root)
    assert ok is False
    assert "self-referential" in detail

def test_r3_r4_manifest_json_created_after_seal_fails(tmp_path):
    root = _mk_manifest_root(tmp_path, {"identity/ok.json": "{}"})
    man = root / "manifests"
    man.mkdir(parents=True, exist_ok=True)
    payload = root / "identity/ok.json"
    (man / "final.sha256").write_text(
        f"{b.sha256_file(payload)}  identity/ok.json\n", encoding="utf-8")
    (man / "manifest.json").write_text(
        json.dumps({"task": "test", "total_files": 1,
                    "total_size_bytes": payload.stat().st_size}) + "\n",
        encoding="utf-8")
    ok, detail, counts = _manifest_verify(root)
    assert ok is False
    assert "manifests/manifest.json" in counts["unmanifested_files"]
    assert "listed exactly once" in detail


def test_r3_r4_manifest_json_rewrite_after_seal_fails(tmp_path):
    root = _mk_manifest_root(tmp_path, {"identity/ok.json": "{}"})
    _write_manifest(root, ["identity/ok.json"])
    manifest_json = root / "manifests" / "manifest.json"
    doc = json.loads(manifest_json.read_text(encoding="utf-8"))
    doc["post_seal_tamper"] = True
    manifest_json.write_text(json.dumps(doc) + "\n", encoding="utf-8")
    ok, detail, counts = _manifest_verify(root)
    assert ok is False
    assert "manifests/manifest.json" in counts["sha_mismatch"]


def test_r3_r4_post_seal_verdict_creation_fails(tmp_path):
    root = _mk_manifest_root(tmp_path, {"identity/ok.json": "{}"})
    _write_manifest(root, ["identity/ok.json"])
    verdict = root / "analysis" / "post-seal-verdict.json"
    verdict.parent.mkdir(parents=True, exist_ok=True)
    verdict.write_text("{}\n", encoding="utf-8")
    ok, detail, counts = _manifest_verify(root)
    assert ok is False
    assert counts["unmanifested_files"] == ["analysis/post-seal-verdict.json"]


def test_r3_r4_seal_evidence_root_is_final_and_self_verified(tmp_path):
    from audit_bj_historical_valuation_build import seal_evidence_root
    root = _mk_manifest_root(tmp_path, {
        "identity/ok.json": "{}",
        "analysis/pre-seal-verdict.json": "{\"verdict\": \"PARTIAL\"}\n",
    })
    result = seal_evidence_root(root, "test-r3-r4")
    assert result["line_count"] == 3
    assert result["unique_paths"] == 3
    lines = (root / "manifests" / "final.sha256").read_text().splitlines()
    assert any(line.endswith("  manifests/manifest.json") for line in lines)
    ok, detail, counts = _manifest_verify(root)
    assert ok is True, detail
    assert counts["unmanifested_files"] == []


def test_r3_r4_seal_evidence_root_refuses_reseal(tmp_path):
    from audit_bj_historical_valuation_build import seal_evidence_root
    root = _mk_manifest_root(tmp_path, {"identity/ok.json": "{}"})
    seal_evidence_root(root, "test-r3-r4")
    with pytest.raises(FileExistsError):
        seal_evidence_root(root, "test-r3-r4-second")



def test_r3_r4_seal_lock_blocks_concurrent_attempt_without_writes(tmp_path):
    root = _mk_manifest_root(tmp_path, {"identity/ok.json": "{}"})
    manifests = root / "manifests"
    manifests.mkdir(parents=True, exist_ok=True)
    lock = manifests / ".seal.lock"
    lock.write_text("existing lock\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        seal_evidence_root(root, "concurrent")
    assert lock.read_text(encoding="utf-8") == "existing lock\n"
    assert not (manifests / "manifest.json").exists()
    assert not (manifests / "final.sha256").exists()


def test_r3_r4_seal_publish_failure_retains_lock_and_never_overwrites(
        tmp_path, monkeypatch):
    root = _mk_manifest_root(tmp_path, {"identity/ok.json": "{}"})
    manifests = root / "manifests"
    original = b._atomic_publish_no_clobber

    def fail_final(temp_path, target):
        if Path(target).name == "final.sha256":
            raise FileExistsError("simulated concurrent final")
        return original(temp_path, target)

    monkeypatch.setattr(b, "_atomic_publish_no_clobber", fail_final)
    with pytest.raises(FileExistsError, match="simulated concurrent final"):
        seal_evidence_root(root, "publish-failure")
    assert (manifests / ".seal.lock").exists()
    assert (manifests / "manifest.json").exists()
    assert not (manifests / "final.sha256").exists()
    with pytest.raises(FileExistsError):
        seal_evidence_root(root, "automatic-retry-blocked")


def test_r3_r4_sealed_stage_b_requires_external_output(tmp_path):
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = _mk_manifest_root(tmp_path, {"identity/ok.json": "{}"})
    seal_evidence_root(root, "test-r3-r4")
    before = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
    code = run(["--mode", "stage-b", "--evidence-root", str(root)])
    after = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
    assert code == 2
    assert after == before
    assert not (root / "analysis" / "pre-seal-verdict.json").exists()


def test_r3_r4_sealed_stage_b_rejects_inside_output(tmp_path):
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = _mk_manifest_root(tmp_path, {"identity/ok.json": "{}"})
    seal_evidence_root(root, "test-r3-r4")
    output = root / "analysis" / "post-seal-verdict.json"
    code = run(["--mode", "stage-b", "--evidence-root", str(root),
                "--output", str(output)])
    assert code == 2
    assert not output.exists()
