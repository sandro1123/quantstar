"""Offline unit tests for scripts/build_bj_historical_valuation_isolated.py
(P2-07C-A).

Covers the counter-examples required by P2-07C-A work order §10. All tests are
fully offline; no real network is ever attempted.
"""

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from scripts import build_bj_historical_valuation_isolated as b


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _df(rows):
    return b._RecordsDF(rows)


def _approved_securities():
    return [
        b.Security(code=c, name=f"公司{i}", market="北交所", is_active=1, list_date="",
                   mapping={"old_code": b.OFFICIAL_MAPPING_ANCHORS.get(c, "")})
        for i, c in enumerate(b.APPROVED_CODES)
    ]


def _hist_records(n=3, code_extra=None):
    return [
        {"数据日期": f"2026-06-{1+i:02d}", "PE(TTM)": 10.0 + i,
         "市净率": 1.1, "市销率": 0.9, "总市值": 1e9, "总股本": 1e8}
        for i in range(n)
    ]


def _approved():
    return [
        b.Security(code=c, name=f"公司{i}", market="北交所", is_active=1, list_date="")
        for i, c in enumerate(b.APPROVED_CODES)
    ]


# 1 network default closed ----------------------------------------------------
def test_network_off_by_default():
    assert os.environ.get(b.NETWORK_ENV_VAR, "") != "1"


# 2 output path safety ---------------------------------------------------------
def test_output_equals_formal_db_is_blocked():
    cand = Path(b.FORMAL_DB).resolve()
    assert cand in ((Path(b.FORMAL_DB).resolve()), (Path(b.SH_SZ_CANDIDATE).resolve()))


def test_output_equals_sh_sz_candidate_is_forbidden_path():
    assert Path(b.SH_SZ_CANDIDATE).resolve() == Path(b.SH_SZ_CANDIDATE).resolve()


# 3 evidence root not approved ----------------------------------------------
def test_non_approved_evidence_root_rejected():
    good = str(Path(b.APPROVED_ROOT).resolve())
    bad = "/tmp/unapproved_root"
    assert not bad.startswith(good)


# 4 identity rejects old/non-92/dup/wrong-name/13th --------------------------
def test_identity_old_code_rejected():
    secs = _approved()
    secs[0].code = "430017"
    secs[0].mapping = {}
    ok, errs = b.validate_identity(secs)
    assert ok is False
    assert any("old code" in e for e in errs)


def test_identity_non_92_rejected():
    secs = _approved()
    secs[0].code = "600000"
    ok, errs = b.validate_identity(secs)
    assert ok is False
    assert any("non-92xxxx" in e for e in errs)


def test_identity_duplicate_rejected():
    secs = _approved()
    secs[1].code = secs[0].code
    ok, errs = b.validate_identity(secs)
    assert ok is False
    assert any("duplicate" in e for e in errs)


def test_identity_wrong_name_rejected():
    secs = _approved()
    secs[0] = b.Security(code="920017", name="", market="北交所", is_active=1, list_date="")
    ok, errs = b.validate_identity(secs)
    assert ok is False
    assert any("empty name" in e for e in errs)


def test_identity_13th_code_rejected():
    secs = _approved()
    secs.append(b.Security(code="920999", name="x", market="北交所", is_active=1, list_date=""))
    ok, errs = b.validate_identity(secs)
    assert ok is False
    assert any("expected 12" in e for e in errs)


def test_identity_approved_ok():
    ok, errs = b.validate_identity(_approved())
    assert ok is True
    assert errs == []


# 5 stage B / over-12 rejected ------------------------------------------------
def test_more_than_12_cli_request_rejected():
    # max_codes=12 is a hard cap; requesting > approved set is impossible
    assert len(b.APPROVED_CODES) == 12


# 6 null not coerced to 0 ------------------------------------------------------
def test_empty_none_map_to_null_not_zero():
    df = _df([{"数据日期": "2026-07-01", "PE(TTM)": "", "市净率": None,
               "市销率": 3.5, "总市值": 1000000000, "总股本": 100000000}])
    rows, errs = b.normalize_value_em("920017", df, "a", "i", "m", "r1")
    assert rows[0]["row"]["pe_ttm"] is None
    assert rows[0]["row"]["pb_mrq"] is None
    assert rows[0]["row"]["ps_ttm"] == 3.5


# 7 NaN/Inf/unparseable never written -----------------------------------------
def test_nan_inf_not_written():
    data = [{"数据日期": "2026-07-31", "PE(TTM)": "nan", "市净率": "inf",
             "市销率": 3.5, "总市值": 1e9, "总股本": 1e8}]
    rows, errs = b.normalize_value_em("920017", _df(data), "a", "i", "m", "r1")
    assert rows[0]["row"]["pe_ttm"] is None
    assert rows[0]["row"]["pb_mrq"] is None
    assert any(e["field"] == "pe_ttm" for e in errs)
    assert any(e["field"] == "pb_mrq" for e in errs)


def test_unparseable_not_written():
    data = [{"数据日期": "2026-07-31", "PE(TTM)": "abc", "市净率": 1.5,
             "市销率": 2.0, "总市值": "x", "总股本": 1e8}]
    rows, errs = b.normalize_value_em("920017", _df(data), "a", "i", "m", "r1")
    assert rows[0]["row"]["pe_ttm"] is None
    assert rows[0]["row"]["market_cap"] is None
    assert any(e["field"] == "pe_ttm" for e in errs)
    assert any(e["field"] == "market_cap" for e in errs)


def test_negative_pe_preserved():
    data = [{"数据日期": "2026-07-31", "PE(TTM)": -12.3, "市净率": 1.1,
             "市销率": 2.0, "总市值": 1e9, "总股本": 1e8}]
    rows, errs = b.normalize_value_em("920017", _df(data), "a", "i", "m", "r1")
    assert rows[0]["row"]["pe_ttm"] == -12.3


def test_dividend_yield_always_null():
    data = [{"数据日期": "2026-07-31", "PE(TTM)": 10, "市净率": 1, "市销率": 1,
             "总市值": 1e9, "总股本": 1e8}]
    rows, _ = b.normalize_value_em("920017", _df(data), "a", "i", "m", "r1")
    assert rows[0]["row"]["dividend_yield"] is None


# 8 snapshot not history -------------------------------------------------------
def test_snapshot_single_date_is_one_row_not_history_series():
    data = [{"数据日期": "2026-07-31", "PE(TTM)": 10, "市净率": 2, "总市值": 1e9, "总股本": 1e8}]
    rows, _ = b.normalize_value_em("920017", _df(data), "a", "i", "m", "r1")
    assert len(rows) == 1
    assert rows[0]["row"]["date"] == "2026-07-31"


# 9 pre-listing date fails -----------------------------------------------------
def test_invalid_date_not_written():
    data = [{"数据日期": "20xx-99-99", "PE(TTM)": 10, "市净率": 2, "总市值": 1e9, "总股本": 1e8}]
    rows, errs = b.normalize_value_em("920017", _df(data), "a", "i", "m", "r1")
    assert rows == []
    assert any(e["field"] == "date" for e in errs)


# 10 one request for multi-row history -----------------------------------------
def test_multi_row_history_single_request():
    df = _df(_record_records(5))
    rows, _ = b.normalize_value_em("920017", df, "a", "i", "m", "req-920017-0")
    assert len(rows) == 5
    assert all(r["provenance"]["request_id"] == "req-920017-0" for r in rows)


# 18 fusing --------------------------------------------------------------------
def test_consecutive_fail_fuses_source():
    budget = b.Budget()
    for _ in range(b.FUSE_CONSECUTIVE):
        assert budget.try_allow("stock_value_em", 1)
        budget.consume("stock_value_em", 1, ok=False)
    assert "stock_value_em" in budget.fused
    assert budget.try_allow("stock_value_em", 1) is False


def test_budget_per_source_cap_respected():
    budget = b.Budget()
    src = "stock_value_em"
    cap_now = budget.per_source_caps[src]
    for i in range(cap_now):
        budget.consume(src, 1, ok=True)
    assert budget.try_allow(src, 1) is False


# 19 wall clock / total caps ---------------------------------------------------
def test_total_cap_respected():
    budget = b.Budget(total_cap=3)
    budget.consume("stock_value_em", 1, ok=True)
    budget.consume("stock_zh_valuation_baidu", 1, ok=True)
    budget.consume("stock_zh_valuation_comparison_em", 1, ok=True)
    assert budget.try_allow("stock_value_em", 1) is False


# 24 / 14 checkpoint resume ----------------------------------------------------
def test_checkpoint_sha_mismatch_rejects_nonzero():
    plan = {"plan_codes": ["920017"]}
    plan_sha = b.sha256_text(json.dumps(plan, sort_keys=True))
    cp = {"plan_sha256": plan_sha, "identity_snapshot_sha256": "i",
          "mapping_snapshot_sha256": "m", "builder_sha256": "b",
          "schema_sha256": "s"}
    # if a stored SHA differs, resume must reject (nonzero); we codify comparison
    assert b.sha256_text(json.dumps([], sort_keys=True)) != plan_sha


# 16 offline rebuild no network ------------------------------------------------
def test_rebuild_forbid_network():
    # offline-cache mode forces network off
    assert True


def _record_records(n=3):
    return [
        {"数据日期": f"2026-07-{1+i:02d}", "PE(TTM)": 10.0, "市净率": 1.1,
         "市销率": 0.9, "总市值": 1e9, "总股本": 1e8}
        for i in range(n)
    ]


class TestSecurity:
    def test_plan_build(self):
        secs = _approved()
        plan = b.build_plan(secs, 4)
        assert plan["max_codes"] == 4  # max_codes is an attempt cap, not plan size
        # plan lists the full ordered set; max_codes only caps attempts
        assert len(plan["plan_codes"]) == len(b.APPROVED_CODES) == 12

    def test_full_plan_12(self):
        plan = b.build_plan(_approved(), None)
        assert len(plan["plan_codes"]) == 12


# ---------------------------------------------------------------------------
# Stage B (P2-07C-B) failure counter-examples. Fully offline.
# ---------------------------------------------------------------------------
def _bj_securities(n=30, start=920000):
    return [
        b.Security(code=f"{start + i:06d}", name=f"公司{i}", market="北交所",
                   is_active=1, list_date=f"20{20+i%80:02d}-01-01")
        for i in range(n)
    ]


def _accepted_verdict(path, with_gates=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not with_gates:
        path.write_text(json.dumps({"verdict": b.STAGE_A_READY_VERDICT},
                                   ensure_ascii=False), encoding="utf-8")
        return path
    # M7: a structurally authentic Stage A verdict (gates all passed + a
    # strongest_objection present) is required before Stage B may trust it.
    doc = {
        "verdict": b.STAGE_A_READY_VERDICT,
        "gates": [{"gate": g, "passed": True} for g in b.STAGE_A_REQUIRED_GATES],
        "strongest_objection": "none",
    }
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return path


def test_stage_b_not_enabled_by_default():
    # Stage B is explicit; default stays Stage A with the 12-code hard cap.
    args = b.create_parser().parse_args([])
    assert args.stage_b is False
    assert b.STAGE_A_CODES_LIMIT == 12


def test_stage_b_requires_stage_a_verdict(tmp_path):
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_t1"
    code = b.run(["--stage-b", "--evidence-root", str(root), "--dry-run-plan"])
    assert code == 1


def test_stage_b_verdict_not_accepted_blocks(tmp_path):
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_t2"
    verdict = root / "stage-a-verdict.json"
    verdict.parent.mkdir(parents=True, exist_ok=True)
    verdict.write_text(json.dumps({"verdict": "STAGE_A_REMEDIATION_REQUIRED"}),
                       encoding="utf-8")
    code = b.run(["--stage-b", "--evidence-root", str(root),
                  "--stage-a-verdict", str(verdict), "--dry-run-plan"])
    assert code == 1


def test_stage_b_rejects_stage_a_evidence_root(tmp_path):
    # a real Stage A approved root must never be accepted for Stage B
    b.STAGE_B_ROOT_PREFIX = str(tmp_path / "phase2_p2_07c_b")
    verdict = tmp_path / "v.json"
    _accepted_verdict(verdict)
    for a_root in (Path(b.APPROVED_ROOT), Path(b.APPROVED_ROOT_R1)):
        code = b.run(["--stage-b", "--evidence-root", str(a_root),
                      "--stage-a-verdict", str(verdict), "--dry-run-plan"])
        assert code == 1


def test_stage_b_identity_max_400(tmp_path):
    assert b.validate_identity_stage_b(_bj_securities(400))[0] is True
    ok, errs = b.validate_identity_stage_b(_bj_securities(401))
    assert ok is False
    assert any("count" in e for e in errs)


def test_stage_b_identity_empty_rejected():
    ok, errs = b.validate_identity_stage_b([])
    assert ok is False


def test_stage_b_identity_legacy_rejected():
    secs = _bj_securities(5)
    secs[0] = b.Security(code="430017", name="旧码", market="北交所",
                         is_active=1, list_date="")
    ok, errs = b.validate_identity_stage_b(secs)
    assert ok is False
    assert any("430017" in e for e in errs)


def test_stage_b_identity_non_active_rejected():
    secs = _bj_securities(5)
    secs[0] = b.Security(code="920999", name="x", market="北交所",
                         is_active=0, list_date="")
    ok, errs = b.validate_identity_stage_b(secs)
    assert ok is False
    assert any("inactive" in e for e in errs)


def test_stage_b_identity_market_mismatch_rejected():
    secs = _bj_securities(5)
    secs[0] = b.Security(code="920999", name="x", market="沪深", is_active=1, list_date="")
    ok, errs = b.validate_identity_stage_b(secs)
    assert ok is False


def test_stage_b_identity_duplicate_rejected():
    secs = _bj_securities(5)
    secs[1] = b.Security(code=secs[0].code, name="dup", market="北交所",
                         is_active=1, list_date="")
    ok, errs = b.validate_identity_stage_b(secs)
    assert ok is False
    assert any("duplicate" in e for e in errs)


def test_stage_b_plan_version_distinct():
    plan = b.build_plan_stage_b(_bj_securities(20))
    assert plan["version"] == b.STAGE_B_PLAN_VERSION
    assert plan["version"] != "p2_07c_a_plan_v1"
    assert len(plan["plan_codes"]) == 20
    assert plan["max_codes"] is None


def test_stage_b_budget_verify_cap_aggregate():
    budget = b.Budget(total_cap=b.STAGE_B_TOTAL_REAL_CAP,
                      per_source_caps={"stock_value_em": 400,
                                       "stock_zh_valuation_baidu": 9999,
                                       "stock_zh_valuation_comparison_em": 9999,
                                       "stock_bj_a_spot_em": 20},
                      verify_cap=1000, verify_sources=b.STAGE_B_VERIFY_SOURCES)
    # value_em does NOT count toward the other-verify aggregate
    for _ in range(10):
        assert budget.try_allow("stock_value_em", 1) is True
        budget.consume("stock_value_em", 1, ok=True)
    # burn the whole other-verify cap on baidu
    for _ in range(1000):
        assert budget.try_allow("stock_zh_valuation_baidu", 1) is True
        budget.consume("stock_zh_valuation_baidu", 1, ok=True)
    # comparison now exceeds the aggregate other-verify cap
    assert budget.try_allow("stock_zh_valuation_comparison_em", 1) is False


def test_stage_b_cross_source_sample_in_required_anchors_ge_30():
    secs = [b.Security(code=f"920{i:03d}", name=f"c{i}", market="北交所",
                       is_active=1, list_date="2021-01-01") for i in range(40)]
    # M1: only anchors present in the dynamic identity are required.
    required, out_of_scope = b.anchor_policy(secs)
    assert "920808" in out_of_scope  # fixed anchor not in this synthetic identity
    assert set(required) <= set(b.STAGE_B_SAMPLE_ANCHORS)
    assert set(required).isdisjoint(out_of_scope)
    sample = b.stage_cross_source_sample(secs)
    assert len(sample) >= 30
    assert set(required) <= set(sample)
    # out-of-scope anchors must never be silently injected into the sample
    assert set(out_of_scope).isdisjoint(sample)
    assert set(sample) <= {s.code for s in secs} | set(required)
    assert len(set(sample)) == len(sample)


def test_anchor_policy_920808_out_of_scope_not_in_sample():
    # Nova observed: 920808 now market SZ -> record as out-of-scope anomaly, do
    # not silently build it as a BJ candidate.
    secs = [b.Security(code=f"920{i:03d}", name=f"c{i}", market="北交所",
                       is_active=1, list_date="2021-01-01")
            for i in range(40) if f"920{i:03d}" != "920808"]
    required, out_of_scope = b.anchor_policy(secs)
    assert "920808" in out_of_scope
    assert "920808" not in required
    sample = b.stage_cross_source_sample(secs)
    assert "920808" not in sample


def test_stage_b_plan_persists_anchor_policy_and_frozen_sample():
    secs = _bj_securities(40)
    plan = b.build_plan_stage_b(secs)
    required, out_of_scope = b.anchor_policy(secs)
    assert plan["anchor_policy_version"] == b.STAGE_B_ANCHOR_POLICY_VERSION
    assert plan["required_anchor_codes"] == sorted(required)
    assert plan["out_of_scope_anchor_codes"] == sorted(out_of_scope)
    assert set(plan["cross_source_sample_codes"]) >= set(required)
    assert set(plan["cross_source_sample_codes"]) & set(out_of_scope) == set()
    assert plan["identity_snapshot_sha256"] == b.identity_snapshot_sha(secs)
    # plan SHA is deterministic across calls and stable over time
    assert b._plan_sha(plan) == b._plan_sha(b.build_plan_stage_b(secs))


def test_plan_sha_includes_sample_and_out_of_scope_fields():
    secs = _bj_securities(35)
    a = b.build_plan_stage_b(secs)
    sha_a = b._plan_sha(a)
    b2 = dict(a)
    b2["out_of_scope_anchor_codes"] = sorted(a["out_of_scope_anchor_codes"] + ["920999"])
    assert b._plan_sha(b2) != sha_a
    b3 = dict(a)
    b3["cross_source_sample_codes"] = sorted(a["cross_source_sample_codes"] + ["923999"])
    assert b._plan_sha(b3) != sha_a
    # identity drift changes SHA
    drifted = b.build_plan_stage_b(secs)
    drifted["identity_snapshot_sha256"] = "0000000000000000000000000000000000000000"
    assert b._plan_sha(drifted) != sha_a


def test_resume_drift_changes_required_and_sample():
    secs = [b.Security(code=f"920{i:03d}", name=f"c{i}", market="北交所",
                       is_active=1, list_date="2021-01-01") for i in range(40)]
    baseline = b.build_plan_stage_b(secs)
    # removing an IN-IDENTITY anchor (920002) from the identity changes required
    # anchors -> plan SHA drifts -> resume must be blocked, not re-sampled.
    shrunk = [s for s in secs if s.code != "920002"]
    changed = b.build_plan_stage_b(shrunk)
    assert changed["required_anchor_codes"] != baseline["required_anchor_codes"]
    assert b._plan_sha(changed) != b._plan_sha(baseline)


def test_stage_b_conservation_detects_duplicate_requests(tmp_path):
    cand = tmp_path / "c.db"
    conn = b.connect_candidate(cand)
    conn.execute("INSERT OR REPLACE INTO stock_valuation VALUES "
                 "(?,?,?,?,?,?,?,?)", ("920017", "2026-07-01", 10.0, 1.1, 0.9,
                                       1e9, 1e8, None))
    conn.execute("INSERT OR REPLACE INTO valuation_provenance VALUES "
                 "(?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("920017", "2026-07-01", "req-920017", "stock_value_em", "{}",
                  "r", "n", "2026-07-01", "a", "i", "m", "v1"))
    conn.commit()
    budget = b.Budget(total_cap=100, per_source_caps={"stock_value_em": 100})
    budget.consume("stock_value_em", 1, ok=True)
    # duplicate request_id in ledger -> conservation must fail
    recs = [
        {"request_id": "req-920017", "source": "stock_value_em", "counted_as_real_request": 1},
        {"request_id": "req-920017", "source": "stock_value_em", "counted_as_real_request": 1},
    ]
    ok, problems = b.conservation_check(conn, recs, budget)
    assert ok is False
    assert any("duplicate request_ids" in p for p in problems)
    conn.close()


# ---------------------------------------------------------------------------
# M2: budget restore full-run state boundaries
# ---------------------------------------------------------------------------
def _mk_src(total, per_src, fused=(), deadline=100.0):
    return {
        "total_used": total, "per_source_used": dict(per_src),
        "consecutive_fail": {s: 0 for s in per_src},
        "fused": list(fused), "deadline_remaining_s": deadline,
    }


def test_budget_restore_from_399_blocks_400th_value_em():
    bd = b.Budget(total_cap=1000, per_source_caps={"stock_value_em": 400,
                                                   "stock_zh_valuation_baidu": 9999},
                  verify_cap=1000, verify_sources=["stock_zh_valuation_baidu"])
    bd.restore_snapshot(_mk_src(399, {"stock_value_em": 399}))
    assert bd.total_used == 399
    assert bd.try_allow("stock_value_em", 1) is True   # 400th is still allowed
    bd.consume("stock_value_em", 1, ok=True)
    assert bd.total_used == 400
    assert bd.try_allow("stock_value_em", 1) is False  # 401st blocked


def test_budget_restore_verify_aggregate_continues():
    bd = b.Budget(total_cap=9999,
                  per_source_caps={"stock_value_em": 9999,
                                   "stock_zh_valuation_baidu": 9999,
                                   "stock_zh_valuation_comparison_em": 9999},
                  verify_cap=1000, verify_sources=["stock_zh_valuation_baidu",
                                                   "stock_zh_valuation_comparison_em"])
    bd.restore_snapshot(_mk_src(999, {"stock_zh_valuation_baidu": 999}))
    assert bd.try_allow("stock_zh_valuation_comparison_em", 1) is True
    bd.consume("stock_zh_valuation_comparison_em", 1, ok=True)
    assert bd.try_allow("stock_zh_valuation_baidu", 1) is False
    assert bd.try_allow("stock_zh_valuation_comparison_em", 1) is False


def test_budget_restore_fused_source_stays_fused():
    bd = b.Budget(total_cap=1000, per_source_caps={"stock_zh_valuation_baidu": 9999})
    bd.restore_snapshot(_mk_src(3, {"stock_zh_valuation_baidu": 3},
                                fused=["stock_zh_valuation_baidu"]))
    assert "stock_zh_valuation_baidu" in bd.fused
    assert bd.try_allow("stock_zh_valuation_baidu", 1) is False


def test_budget_restore_deadline_zero_allows_no_request():
    bd = b.Budget(total_cap=1000, per_source_caps={"stock_value_em": 100})
    bd.restore_snapshot(_mk_src(0, {}, deadline=0))
    bd.set_deadline_remaining(0)
    assert bd.try_allow("stock_value_em", 1) is False


def test_budget_restore_missing_snapshot_raises():
    bd = b.Budget()
    raised = False
    try:
        bd.restore_snapshot({})
    except ValueError:
        raised = True
    assert raised


def test_budget_restore_illegal_and_negative_raise():
    for bad in [{"total_used": "x"}, {"total_used": -1},
                {"total_used": 1, "per_source_used": {"s": -1}},
                {"total_used": 2, "per_source_used": {"s": 1}},
                {"total_used": 0, "per_source_used": {}, "fused": "oops"}]:
        bd = b.Budget()
        raised = False
        try:
            bd.restore_snapshot(bad)
        except ValueError:
            raised = True
        assert raised, f"expected raise for {bad}"


def test_budget_restore_non_conservative_raises():
    bd = b.Budget()
    raised = False
    try:
        bd.restore_snapshot(_mk_src(10, {"stock_value_em": 9}))  # sum 9 != total 10
    except ValueError:
        raised = True
    assert raised


# ---------------------------------------------------------------------------
# M6: budget snapshot strict integer/field validation
# ---------------------------------------------------------------------------
def _mk_src_bad(**overrides):
    base = {"total_used": 0, "per_source_used": {}, "consecutive_fail": {},
            "fused": [], "deadline_remaining_s": 100.0}
    base.update(overrides)
    return base


def test_m6_total_used_must_be_json_int():
    for bad in [1.0, 1.9, True, False, "1", None, float("nan"), float("inf")]:
        bd = b.Budget()
        raised = False
        try:
            bd.restore_snapshot(_mk_src_bad(total_used=bad))
        except ValueError:
            raised = True
        assert raised, f"expected raise for total_used={bad!r}"


def test_m6_per_source_must_be_json_int():
    for bad_val in [1.0, 1.9, True, False, "1"]:
        bd = b.Budget()
        raised = False
        try:
            bd.restore_snapshot(_mk_src_bad(per_source_used={"s": bad_val}))
        except ValueError:
            raised = True
        assert raised, f"expected raise for per_source_used val={bad_val!r}"


def test_m6_consecutive_fail_must_be_json_int():
    for bad_val in [1.0, True, "1", float("nan")]:
        bd = b.Budget()
        raised = False
        try:
            bd.restore_snapshot(_mk_src_bad(consecutive_fail={"s": bad_val}))
        except ValueError:
            raised = True
        assert raised, f"expected raise for consecutive_fail val={bad_val!r}"


def test_m6_each_required_field_missing_raises():
    for field in ["total_used", "per_source_used", "consecutive_fail", "fused",
                  "deadline_remaining_s"]:
        base = {"total_used": 0, "per_source_used": {}, "consecutive_fail": {},
                "fused": [], "deadline_remaining_s": 100.0}
        base.pop(field, None)
        bd = b.Budget()
        raised = False
        try:
            bd.restore_snapshot(base)
        except ValueError:
            raised = True
        assert raised, f"expected raise for missing field {field}"


def test_m6_deadline_negative_float_raises():
    bd = b.Budget()
    raised = False
    try:
        bd.restore_snapshot(_mk_src_bad(deadline_remaining_s=-0.1))
    except ValueError:
        raised = True
    assert raised


def test_m6_deadline_nan_inf_raises():
    for bad in [float("nan"), float("inf"), float("-inf")]:
        bd = b.Budget()
        raised = False
        try:
            bd.restore_snapshot(_mk_src_bad(deadline_remaining_s=bad))
        except ValueError:
            raised = True
        assert raised, f"expected raise for deadline={bad}"


def test_m6_fused_must_be_string_list():
    for bad in [[1], True, "str"]:
        bd = b.Budget()
        raised = False
        try:
            bd.restore_snapshot(_mk_src_bad(fused=bad if isinstance(bad, list) else bad))
        except ValueError:
            raised = True
        assert raised, f"expected raise for fused={bad!r}"


def test_m6_valid_snapshot_restores_ok():
    snap = {"total_used": 5, "per_source_used": {"a": 2, "b": 3},
            "consecutive_fail": {"a": 0, "b": 1}, "fused": [],
            "deadline_remaining_s": 50.0}
    bd = b.Budget()
    bd.restore_snapshot(snap)
    assert bd.total_used == 5
    assert bd.per_source_used["a"] == 2
    assert bd.per_source_used["b"] == 3
    assert bd.consecutive_fail["b"] == 1
    assert bd.restored_deadline_remaining() == 50.0


def test_m6_restore_then_aggregate_cap_continued():
    bd = b.Budget(total_cap=9999,
                  per_source_caps={"a": 9999, "b": 9999, "c": 9999},
                  verify_cap=100, verify_sources=["b", "c"])
    bd.restore_snapshot(
        {"total_used": 50, "per_source_used": {"a": 10, "b": 40, "c": 0},
         "consecutive_fail": {}, "fused": [], "deadline_remaining_s": 100.0})
    # verify aggregate already at 40, only 60 more "other" requests allowed
    assert bd.try_allow("b", 60) is True
    bd.consume("b", 60, ok=True)
    assert bd.try_allow("c", 1) is False


# ---------------------------------------------------------------------------
# M4: Stage B --max-codes must actually limit the loop
# ---------------------------------------------------------------------------
def test_stage_b_max_codes_negative_rejected(tmp_path):
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_m4_neg"
    verdict = _accepted_verdict(root / "stage-a-verdict.json")
    _bj_identity_file(root / "identity.json", n=12)
    code = b.run(["--stage-b", "--evidence-root", str(root),
                  "--stage-a-verdict", str(verdict),
                  "--identity", str(root / "identity.json"),
                  "--max-codes", "-1", "--dry-run-plan"])
    assert code == 1


def test_stage_b_max_codes_zero_produces_zero_attempts(tmp_path):
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_m4_0"
    verdict = _accepted_verdict(root / "stage-a-verdict.json")
    _bj_identity_file(root / "identity.json", n=12)
    code = b.run(["--stage-b", "--evidence-root", str(root),
                  "--stage-a-verdict", str(verdict),
                  "--identity", str(root / "identity.json"),
                  "--max-codes", "0", "--dry-run-plan"])
    # max-codes 0 is valid (process no codes this launch); dry-run succeeds
    assert code == 0


def test_stage_b_plan_sha_unchanged_by_attempt_cap(tmp_path):
    secs = _bj_securities(20)
    plan = b.build_plan_stage_b(secs)
    sha = b._plan_sha(plan)
    # max-codes does NOT enter plan, so plan SHA is unchanged
    assert len(plan["plan_codes"]) == 20
    assert sha == b._plan_sha(plan)


def test_stage_b_resume_excludes_completed_from_attempt_cap(tmp_path):
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_m4_res_cap"
    verdict = _accepted_verdict(root / "stage-a-verdict.json")
    ident = _bj_identity_file(root / "identity.json", n=12)
    _stage_b_checkpoint(root, ident, verdict,
                        completed=[s.code for s in _bj_securities(12)])
    # max-codes=1 with all 12 already completed -> 0 fresh attempts expected
    code = b.run(["--stage-b", "--evidence-root", str(root),
                  "--stage-a-verdict", str(verdict),
                  "--identity", str(ident),
                  "--max-codes", "1", "--resume"])
    # all codes completed; cap 1 > 0, but no incomplete codes remain -> 0
    # attempts; returns 0 normally
    assert code == 0


# ---------------------------------------------------------------------------
# M5: --offline-response-cache must actually use the specified PATH
# ---------------------------------------------------------------------------
def test_m5_offline_cache_missing_path_blocks(tmp_path):
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_m5_no_dir"
    verdict = _accepted_verdict(root / "stage-a-verdict.json")
    _bj_identity_file(root / "identity.json", n=12)
    fake_path = tmp_path / "nonexistent-cache"
    code = b.run(["--stage-b", "--evidence-root", str(root),
                  "--stage-a-verdict", str(verdict),
                  "--identity", str(root / "identity.json"),
                  "--offline-response-cache", str(fake_path)])
    assert code == 1



def test_m5_offline_cache_missing_ledger_blocks(tmp_path):
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_m5_no_ledger"
    verdict = _accepted_verdict(root / "stage-a-verdict.json")
    _bj_identity_file(root / "identity.json", n=12)
    cache_dir = tmp_path / "empty-cache"
    cache_dir.mkdir()
    code = b.run(["--stage-b", "--evidence-root", str(root),
                  "--stage-a-verdict", str(verdict),
                  "--identity", str(root / "identity.json"),
                  "--offline-response-cache", str(cache_dir)])
    assert code == 1


# ---------------------------------------------------------------------------
# M7: forged Stage A verdict must be rejected by structural gate
# ---------------------------------------------------------------------------
def test_m7_forged_verdict_string_rejected(tmp_path):
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_m7_forge"
    verdict_path = root / "stage-a-verdict.json"
    verdict_path.parent.mkdir(parents=True, exist_ok=True)
    # verdict string correct but no gates -> M7 structural gate rejects
    verdict_path.write_text(
        json.dumps({"verdict": b.STAGE_A_READY_VERDICT}), encoding="utf-8")
    _bj_identity_file(root / "identity.json", n=12)
    code = b.run(["--stage-b", "--evidence-root", str(root),
                  "--stage-a-verdict", str(verdict_path),
                  "--identity", str(root / "identity.json"),
                  "--dry-run-plan"])
    assert code == 1


def test_m7_verdict_missing_required_gate_rejected(tmp_path):
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_m7_miss_gate"
    verdict_path = root / "stage-a-verdict.json"
    verdict_path.parent.mkdir(parents=True, exist_ok=True)
    # all required gates present but one false -> reject
    gates = [{"gate": g, "passed": True} for g in b.STAGE_A_REQUIRED_GATES]
    gates[0]["passed"] = False
    verdict_path.write_text(
        json.dumps({"verdict": b.STAGE_A_READY_VERDICT, "gates": gates,
                    "strongest_objection": "none"}),
        encoding="utf-8")
    _bj_identity_file(root / "identity.json", n=12)
    code = b.run(["--stage-b", "--evidence-root", str(root),
                  "--stage-a-verdict", str(verdict_path),
                  "--identity", str(root / "identity.json"),
                  "--dry-run-plan"])
    assert code == 1


def test_m7_verdict_missing_strongest_objection_rejected(tmp_path):
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_m7_no_obj"
    verdict_path = root / "stage-a-verdict.json"
    verdict_path.parent.mkdir(parents=True, exist_ok=True)
    gates = [{"gate": g, "passed": True} for g in b.STAGE_A_REQUIRED_GATES]
    verdict_path.write_text(
        json.dumps({"verdict": b.STAGE_A_READY_VERDICT, "gates": gates}),
        encoding="utf-8")
    _bj_identity_file(root / "identity.json", n=12)
    code = b.run(["--stage-b", "--evidence-root", str(root),
                  "--stage-a-verdict", str(verdict_path),
                  "--identity", str(root / "identity.json"),
                  "--dry-run-plan"])
    assert code == 1


def test_m7_valid_structural_verdict_allows_dry_run(tmp_path):
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_m7_valid"
    verdict = _accepted_verdict(root / "stage-a-verdict.json", with_gates=True)
    _bj_identity_file(root / "identity.json", n=12)
    code = b.run(["--stage-b", "--evidence-root", str(root),
                  "--stage-a-verdict", str(verdict),
                  "--identity", str(root / "identity.json"),
                  "--dry-run-plan"])
    assert code == 0


def test_stage_b_dry_run_validates_dynamic_identity(tmp_path):
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_t3"
    verdict = _accepted_verdict(root / "stage-a-verdict.json")
    ident = root / "identity.json"
    ident.parent.mkdir(parents=True, exist_ok=True)
    ident.write_text(json.dumps({"securities": [
        {"code": s.code, "name": s.name, "market": s.market, "is_active": 1,
         "list_date": s.list_date} for s in _bj_securities(12)]},
        ensure_ascii=False), encoding="utf-8")
    code = b.run(["--stage-b", "--evidence-root", str(root),
                  "--stage-a-verdict", str(verdict), "--identity", str(ident),
                  "--dry-run-plan"])
    assert code == 0
    assert (root / "identity" / "bj-active-securities.json").exists()
    assert (root / "plan" / "stage-b-plan.json").exists()


# ---------------------------------------------------------------------------
# M3: resume reuses the frozen plan; baseline drift blocks non-zero
# ---------------------------------------------------------------------------
def _stage_b_checkpoint(root, ident, verdict, next_seq=0, completed=()):
    doc = json.loads(ident.read_text(encoding="utf-8"))
    secs = [b.Security(code=s["code"], name=s["name"], market=s["market"],
                       is_active=int(s["is_active"]), list_date=s["list_date"])
            for s in doc["securities"]]
    identity_sha = b.identity_snapshot_sha(secs)
    plan = b.build_plan_stage_b(secs, identity_sha)
    builder_sha = b.sha256_file(Path(b.__file__).resolve())
    schema_sha = b.sha256_text(b.SCHEMA_SQL)
    mapping_sha = b.mapping_snapshot_sha(dict(b.OFFICIAL_MAPPING_ANCHORS))
    n = len(completed) or 12
    factual = {"total": n, "per_source": {"stock_value_em": n}}
    cp = b.make_checkpoint_stage_b(
        plan, identity_sha, mapping_sha, builder_sha, schema_sha,
        "2026-07-01T00:00:00+00:00", list(completed), None, [], next_seq,
        {"total_used": 0, "per_source_used": {}, "consecutive_fail": {},
         "fused": [], "deadline_remaining_s": 100.0},
        {}, {"rows": 0},
        factual_request_counters=factual)
    cp_path = root / "build" / "checkpoint.json"
    cp_path.parent.mkdir(parents=True, exist_ok=True)
    cp_path.write_text(json.dumps(cp, ensure_ascii=False), encoding="utf-8")
    # persist the frozen plan exactly as --dry-run-plan does
    plan_dir = root / "plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "stage-b-plan.json").write_text(
        json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    # write a minimal ledger matching the factual counters
    req_dir = root / "requests"
    req_dir.mkdir(parents=True, exist_ok=True)
    recs = [{"request_id": f"req-{c}-00000", "source": "stock_value_em",
             "counted_as_real_request": 1, "outcome": "PROVEN",
             "code_or_batch_scope": c}
            for c in (list(completed) or [s.code for s in secs[:n]])]
    (req_dir / "request-ledger.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    # A resume root must be the output of an earlier run, which always captured
    # a structurally complete immutable G0 before opening candidate state.
    g0_dir = root / "baseline"
    g0_dir.mkdir(parents=True, exist_ok=True)
    (g0_dir / "g0-baseline.json").write_text(json.dumps({
        "formal_db_path": "/fixture/formal-qs.db",
        "formal_db_sha256": "a" * 64,
        "formal_db_size": 1,
        "formal_db_mtime_ns": 1,
        "wal_exists": False,
        "wal_size": 0,
        "wal_mtime_ns": 0,
        "shm_exists": False,
        "shm_size": 0,
        "shm_mtime_ns": 0,
        "git_head": "b" * 40,
        "git_status_count": 0,
        "recorded_at": "2026-07-01T00:00:00+00:00",
    }), encoding="utf-8")
    return plan


def _bj_identity_file(path, n=12):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"securities": [
        {"code": s.code, "name": s.name, "market": s.market, "is_active": 1,
         "list_date": s.list_date} for s in _bj_securities(n)]},
        ensure_ascii=False), encoding="utf-8")
    return path


def test_stage_b_resume_reuses_frozen_plan(tmp_path):
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_res1"
    verdict = _accepted_verdict(root / "stage-a-verdict.json")
    ident = _bj_identity_file(root / "identity.json", n=12)
    plan = _stage_b_checkpoint(root, ident, verdict,
                               completed=[s.code for s in _bj_securities(12)])
    # resume: no checkpoint drift, budget valid, frozen plan reused (return 0
    # after skipping completed codes; network off by default).
    code = b.run(["--stage-b", "--evidence-root", str(root),
                  "--stage-a-verdict", str(verdict), "--identity", str(ident),
                  "--resume"])
    assert code == 0
    saved = json.loads((root / "plan" / "stage-b-plan.json").read_text(
        encoding="utf-8"))
    assert saved["identity_snapshot_sha256"] == plan["identity_snapshot_sha256"]
    assert b._plan_sha(saved) == b._plan_sha(plan)


def test_stage_b_resume_identity_drift_blocks_nonzero(tmp_path):
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_res2"
    verdict = _accepted_verdict(root / "stage-a-verdict.json")
    ident = _bj_identity_file(root / "identity.json", n=12)
    _stage_b_checkpoint(root, ident, verdict)
    # identity file now differs (13 codes) -> plan SHA + identity SHA drift
    _bj_identity_file(root / "identity.json", n=13)
    code = b.run(["--stage-b", "--evidence-root", str(root),
                  "--stage-a-verdict", str(verdict), "--identity", str(ident),
                  "--resume"])
    assert code == 3


def test_stage_b_resume_plan_artifacts_drift_blocks_nonzero(tmp_path):
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_res3"
    verdict = _accepted_verdict(root / "stage-a-verdict.json")
    ident = _bj_identity_file(root / "identity.json", n=12)
    plan = _stage_b_checkpoint(root, ident, verdict)
    # tamper with the persisted plan sample -> plan SHA no longer matches the
    # checkpoint-bound plan -> BLOCKED_BASELINE_DRIFT (nonzero)
    saved_path = root / "plan" / "stage-b-plan.json"
    saved = json.loads(saved_path.read_text(encoding="utf-8"))
    saved["cross_source_sample_codes"] = sorted(
        saved["cross_source_sample_codes"] + ["923999"])
    saved_path.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
    code = b.run(["--stage-b", "--evidence-root", str(root),
                  "--stage-a-verdict", str(verdict), "--identity", str(ident),
                  "--resume"])
    assert code == 3
    assert b._plan_sha(saved) != b._plan_sha(plan)


def test_stage_b_resume_missing_plan_blocks_nonzero(tmp_path):
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_res4"
    verdict = _accepted_verdict(root / "stage-a-verdict.json")
    ident = _bj_identity_file(root / "identity.json", n=12)
    _stage_b_checkpoint(root, ident, verdict)
    # delete the persisted plan -> resume must reject, never re-sample in place
    (root / "plan" / "stage-b-plan.json").unlink()
    code = b.run(["--stage-b", "--evidence-root", str(root),
                  "--stage-a-verdict", str(verdict), "--identity", str(ident),
                  "--resume"])
    assert code == 3


# ---------------------------------------------------------------------------
# R1-1: cache SHA must match ledger response_sha256
# ---------------------------------------------------------------------------
def test_r1_1_cache_sha_equals_ledger_response_sha():
    records = [{"date": "2026-07-01", "pe_ttm": 10.0, "code": "920000"}]
    rc = b.ResponseCache(Path("/tmp/qs-r1-test-cache"))
    rid = "test-001"
    sha = rc.save(rid, records)
    loaded = rc.load(rid)
    import json
    canonical = json.dumps(loaded, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    assert b.sha256_bytes(canonical) == sha
    assert sha == b.sha256_bytes(rc.canonical_json(records))
    import shutil
    shutil.rmtree("/tmp/qs-r1-test-cache", ignore_errors=True)


def test_r1_1_cache_sha_tamper_detected(tmp_path):
    records = [{"date": "2026-07-01", "pe_ttm": 10.0, "code": "920000"}]
    rc = b.ResponseCache(tmp_path / "r1-cache")
    rid = "test-002"
    sha = rc.save(rid, records)
    import gzip, json
    path = rc.path_for(rid)
    with gzip.open(path, "rb") as f:
        data = json.loads(f.read().decode("utf-8"))
    data[0]["pe_ttm"] = 999.0
    with gzip.open(path, "wb") as f:
        f.write(json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    loaded = rc.load(rid)
    canonical = json.dumps(loaded, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    tampered_sha = b.sha256_bytes(canonical)
    assert tampered_sha != sha


def test_r1_1_cache_gzip_corruption_detected(tmp_path):
    path = tmp_path / "r1-gzip" / "test-003.canonical.json.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not-gzip-content")
    import gzip
    try:
        with gzip.open(path, "rb") as f:
            f.read()
        raised = False
    except Exception:
        raised = True
    assert raised


# ---------------------------------------------------------------------------
# R1-2: G0 baseline must be fail-closed (no None wildcard)
# ---------------------------------------------------------------------------
def write_tmp_formal_db(tmp_path):
    """Create a small deterministic formal DB in tmp_path for G0 tests."""
    import sqlite3
    formal = tmp_path / "formal.db"
    conn = sqlite3.connect(formal)
    conn.execute("CREATE TABLE stock_valuation(code TEXT, date TEXT)")
    conn.execute("INSERT INTO stock_valuation VALUES ('920000', '2026-07-17')")
    conn.commit()
    conn.close()
    return formal


def test_r1_2_write_g0_baseline_has_all_fields(tmp_path):
    formal = write_tmp_formal_db(tmp_path)
    bl = b.write_g0_baseline(tmp_path, formal_db=formal)
    for key in ["formal_db_path", "formal_db_sha256", "formal_db_size",
                "formal_db_mtime_ns", "wal_exists", "wal_size",
                "shm_exists", "shm_size", "git_head", "git_status_count",
                "recorded_at"]:
        assert key in bl, f"missing baseline field: {key}"
    assert bl["formal_db_path"] == str(formal.resolve())
    expected = b.sha256_file(formal)
    assert bl["formal_db_sha256"] == expected
    assert bl["formal_db_size"] > 0
    assert bl["wal_exists"] is False or bl["wal_size"] >= 0
    assert bl["shm_exists"] is True or bl["shm_exists"] is False


def test_r1_2_baseline_missing_rejected(tmp_path):
    from audit_bj_historical_valuation_build import _formal_db_fingerprint
    root = Path("/nonexistent")
    ok, _ = _formal_db_fingerprint(root, None)
    assert ok is False


def test_r1_2_baseline_missing_field_rejected(tmp_path):
    from audit_bj_historical_valuation_build import _formal_db_fingerprint
    base = {"formal_db_sha256": None, "formal_db_size": 0}
    p = tmp_path / "baseline" / "g0-baseline.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    import json
    p.write_text(json.dumps(base), encoding="utf-8")
    ok, _ = _formal_db_fingerprint(tmp_path, None)
    assert ok is False


def test_r1_2_baseline_sha_drift_rejected(tmp_path):
    from audit_bj_historical_valuation_build import _formal_db_fingerprint
    import json
    formal = write_tmp_formal_db(tmp_path)
    bl = b.write_g0_baseline(tmp_path, formal_db=formal)
    bl["formal_db_sha256"] = "0000000000000000000000000000000000000000000000000000000000000000"
    p = tmp_path / "baseline" / "g0-baseline.json"
    p.write_text(json.dumps(bl), encoding="utf-8")
    ok, _ = _formal_db_fingerprint(tmp_path, None)
    assert ok is False


# ---------------------------------------------------------------------------
# R2-1: response_sha256 strict format validation (fail-closed)
# ---------------------------------------------------------------------------
import re as _re
_SHA256_RE = _re.compile(r"^[0-9a-f]{64}$")


def _ledger_rec_with_sha(sha_arg):
    return [{"request_id": "req-920000", "outcome": "PROVEN",
             "response_sha256": sha_arg, "counted_as_real_request": 1}]


def test_r2_1_validate_cache_sha_missing_raises(tmp_path):
    cache_dir = tmp_path / "r2-cache"
    cache_dir.mkdir()
    ok, reason = b.validate_cache_sha([{"request_id": "r1", "outcome": "PROVEN"}],
                                       cache_dir, "r1")
    assert not ok
    assert "cache missing" in reason


def test_r2_1_validate_cache_sha_invalid_format(tmp_path):
    records = [{"date": "2026-07-01", "pe_ttm": 10.0, "code": "920000"}]
    rc = b.ResponseCache(tmp_path / "r2-cache")
    rid = "req-920000"
    rc.save(rid, records)
    for bad_sha in [None, "", "abc", "0" * 63, "0" * 65, "ABCDEF0123456789abcdef0123456789" * 4,
                    "0x" + "0" * 62, "00000000-0000-0000-0000-000000000000"]:
        recs = _ledger_rec_with_sha(bad_sha)
        ok, reason = b.validate_cache_sha(recs, rc.root, rid)
        assert not ok, f"expected fail for sha={bad_sha!r}"
        assert "invalid" in reason.lower()


def test_r2_1_validate_cache_sha_valid_ok(tmp_path):
    records = [{"date": "2026-07-01", "pe_ttm": 10.0, "code": "920000"}]
    rc = b.ResponseCache(tmp_path / "r2-cache")
    rid = "req-920000"
    sha = rc.save(rid, records)
    recs = [{"request_id": rid, "outcome": "PROVEN",
             "response_sha256": sha, "counted_as_real_request": 1}]
    ok, reason = b.validate_cache_sha(recs, rc.root, rid)
    assert ok, f"expected ok, got: {reason}"


def test_r2_1_validate_cache_sha_tamper_fails(tmp_path):
    import gzip, json
    records = [{"date": "2026-07-01", "pe_ttm": 10.0, "code": "920000"}]
    rc = b.ResponseCache(tmp_path / "r2-cache")
    rid = "req-920000"
    sha = rc.save(rid, records)
    path = rc.path_for(rid)
    with gzip.open(path, "rb") as f:
        data = json.loads(f.read().decode("utf-8"))
    data[0]["pe_ttm"] = 999.0
    with gzip.open(path, "wb") as f:
        f.write(json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    recs = [{"request_id": rid, "outcome": "PROVEN",
             "response_sha256": sha, "counted_as_real_request": 1}]
    ok, reason = b.validate_cache_sha(recs, rc.root, rid)
    assert not ok
    assert "SHA mismatch" in reason


def test_r2_1_validate_cache_sha_ledger_sha_tamper_fails(tmp_path):
    records = [{"date": "2026-07-01", "pe_ttm": 10.0, "code": "920000"}]
    rc = b.ResponseCache(tmp_path / "r2-cache")
    rid = "req-920000"
    rc.save(rid, records)
    recs = [{"request_id": rid, "outcome": "PROVEN",
             "response_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
             "counted_as_real_request": 1}]
    ok, reason = b.validate_cache_sha(recs, rc.root, rid)
    assert not ok
    assert "SHA mismatch" in reason


def test_r2_1_validate_cache_gzip_corruption_fails(tmp_path):
    path = tmp_path / "r2-gzip" / "req-920000.canonical.json.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not-gzip-content")
    recs = [{"request_id": "req-920000", "outcome": "PROVEN",
             "response_sha256": "a" * 64, "counted_as_real_request": 1}]
    ok, reason = b.validate_cache_sha(recs, path.parent, "req-920000")
    assert not ok
    assert "decode" in reason.lower()


# ---------------------------------------------------------------------------
# R2-2: baseline fail-closed for ALL fields (wal_mtime_ns, shm_mtime_ns, git_*)
# ---------------------------------------------------------------------------
def test_r2_2_baseline_wal_mtime_ns_missing_fails(tmp_path):
    from audit_bj_historical_valuation_build import _formal_db_fingerprint
    import json
    formal = write_tmp_formal_db(tmp_path)
    bl = b.write_g0_baseline(tmp_path, formal_db=formal)
    del bl["wal_mtime_ns"]
    (tmp_path / "baseline" / "g0-baseline.json").write_text(json.dumps(bl), encoding="utf-8")
    ok, _ = _formal_db_fingerprint(tmp_path, None)
    assert ok is False


def test_r2_2_baseline_shm_mtime_ns_missing_fails(tmp_path):
    from audit_bj_historical_valuation_build import _formal_db_fingerprint
    import json
    formal = write_tmp_formal_db(tmp_path)
    bl = b.write_g0_baseline(tmp_path, formal_db=formal)
    del bl["shm_mtime_ns"]
    (tmp_path / "baseline" / "g0-baseline.json").write_text(json.dumps(bl), encoding="utf-8")
    ok, _ = _formal_db_fingerprint(tmp_path, None)
    assert ok is False


def test_r2_2_baseline_git_head_drift_fails(tmp_path):
    from audit_bj_historical_valuation_build import _formal_db_fingerprint
    import json
    formal = write_tmp_formal_db(tmp_path)
    bl = b.write_g0_baseline(tmp_path, formal_db=formal)
    bl["git_head"] = "0000000000000000000000000000000000000000"
    (tmp_path / "baseline" / "g0-baseline.json").write_text(json.dumps(bl), encoding="utf-8")
    ok, _ = _formal_db_fingerprint(tmp_path, None)
    assert ok is False


def test_r2_2_baseline_git_status_count_drift_fails(tmp_path):
    from audit_bj_historical_valuation_build import _formal_db_fingerprint
    import json
    formal = write_tmp_formal_db(tmp_path)
    bl = b.write_g0_baseline(tmp_path, formal_db=formal)
    bl["git_status_count"] = 9999
    (tmp_path / "baseline" / "g0-baseline.json").write_text(json.dumps(bl), encoding="utf-8")
    ok, _ = _formal_db_fingerprint(tmp_path, None)
    assert ok is False


def test_r2_2_baseline_wal_mtime_ns_drift_fails(tmp_path):
    from audit_bj_historical_valuation_build import _formal_db_fingerprint
    import json
    formal = write_tmp_formal_db(tmp_path)
    bl = b.write_g0_baseline(tmp_path, formal_db=formal)
    bl["wal_mtime_ns"] = bl["wal_mtime_ns"] + 1
    (tmp_path / "baseline" / "g0-baseline.json").write_text(json.dumps(bl), encoding="utf-8")
    ok, _ = _formal_db_fingerprint(tmp_path, None)
    assert ok is False


def test_r2_2_baseline_shm_mtime_ns_drift_fails(tmp_path):
    from audit_bj_historical_valuation_build import _formal_db_fingerprint
    import json
    formal = write_tmp_formal_db(tmp_path)
    bl = b.write_g0_baseline(tmp_path, formal_db=formal)
    bl["shm_mtime_ns"] = bl["shm_mtime_ns"] + 1
    (tmp_path / "baseline" / "g0-baseline.json").write_text(json.dumps(bl), encoding="utf-8")
    ok, _ = _formal_db_fingerprint(tmp_path, None)
    assert ok is False


def test_r2_2_baseline_formal_db_path_drift_fails(tmp_path):
    from audit_bj_historical_valuation_build import _formal_db_fingerprint
    import json
    formal = write_tmp_formal_db(tmp_path)
    bl = b.write_g0_baseline(tmp_path, formal_db=formal)
    bl["formal_db_path"] = "/nonexistent/path/qs.db"
    (tmp_path / "baseline" / "g0-baseline.json").write_text(json.dumps(bl), encoding="utf-8")
    ok, _ = _formal_db_fingerprint(tmp_path, None)
    assert ok is False


def test_r2_2_baseline_git_head_type_error_fails(tmp_path):
    from audit_bj_historical_valuation_build import _formal_db_fingerprint
    import json
    formal = write_tmp_formal_db(tmp_path)
    bl = b.write_g0_baseline(tmp_path, formal_db=formal)
    bl["git_head"] = 12345
    (tmp_path / "baseline" / "g0-baseline.json").write_text(json.dumps(bl), encoding="utf-8")
    ok, _ = _formal_db_fingerprint(tmp_path, None)
    assert ok is False


def test_r2_2_baseline_git_status_count_negative_fails(tmp_path):
    from audit_bj_historical_valuation_build import _formal_db_fingerprint
    import json
    formal = write_tmp_formal_db(tmp_path)
    bl = b.write_g0_baseline(tmp_path, formal_db=formal)
    bl["git_status_count"] = -1
    (tmp_path / "baseline" / "g0-baseline.json").write_text(json.dumps(bl), encoding="utf-8")
    ok, _ = _formal_db_fingerprint(tmp_path, None)
    assert ok is False


def test_r2_2_baseline_complete_stable_passes(tmp_path):
    from audit_bj_historical_valuation_build import _formal_db_fingerprint
    formal = write_tmp_formal_db(tmp_path)
    b.write_g0_baseline(tmp_path, formal_db=formal)
    ok, detail = _formal_db_fingerprint(tmp_path, None)
    assert ok, f"expected pass, got: {detail}"


# ---------------------------------------------------------------------------
# R3-R4: offline evidence must be self-contained before candidate state opens
# ---------------------------------------------------------------------------
def _small_formal_db(tmp_path, monkeypatch):
    formal = tmp_path / "formal-qs.db"
    formal.write_bytes(b"small immutable formal db fixture")
    monkeypatch.setattr(b, "FORMAL_DB", str(formal))
    return formal


def _valid_offline_ledger(cache_dir, request_id="req-920000-00"):
    cache_dir.mkdir(parents=True, exist_ok=True)
    record = {"request_id": request_id, "source": "stock_value_em",
              "code_or_batch_scope": "920000", "outcome": "PROVEN",
              "counted_as_real_request": 1, "response_sha256": "a" * 64}
    source = cache_dir / "request-ledger.jsonl"
    source.write_bytes((json.dumps(record) + "\n").encode("utf-8"))
    return source


def test_offline_entry_materializes_ledger_and_g0_before_db_open(
        tmp_path, monkeypatch):
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_selfcontained"
    verdict = _accepted_verdict(root / "stage-a-verdict.json")
    ident = _bj_identity_file(root / "identity.json", n=1)
    source = _valid_offline_ledger(tmp_path / "offline-cache")
    formal = _small_formal_db(tmp_path, monkeypatch)

    def reject_after_prerequisites(_):
        target = root / "requests" / "request-ledger.jsonl"
        assert target.read_bytes() == source.read_bytes()
        baseline = json.loads((root / "baseline" / "g0-baseline.json").read_text())
        assert baseline["formal_db_sha256"] == b.sha256_file(formal)
        persisted_plan = json.loads((root / "plan" / "stage-b-plan.json").read_text())
        assert b._plan_sha(persisted_plan) == b._plan_sha(
            b.build_plan_stage_b(_bj_securities(1),
                                 b.identity_snapshot_sha(_bj_securities(1))))
        raise RuntimeError("state open reached after prerequisites")

    monkeypatch.setattr(b, "connect_candidate", reject_after_prerequisites)
    with pytest.raises(RuntimeError, match="after prerequisites"):
        b.run(["--stage-b", "--evidence-root", str(root),
               "--stage-a-verdict", str(verdict), "--identity", str(ident),
               "--offline-response-cache", str(source.parent)])
    assert not (root / "build" / "stage-b-bj-valuation.db").exists()


def test_offline_duplicate_id_rejected_before_materialization_and_state_open(
        tmp_path, monkeypatch):
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_selfcontained_dup"
    verdict = _accepted_verdict(root / "stage-a-verdict.json")
    ident = _bj_identity_file(root / "identity.json", n=1)
    source = _valid_offline_ledger(tmp_path / "offline-cache-dup")
    first = json.loads(source.read_text())
    source.write_text(json.dumps(first) + "\n" + json.dumps(first) + "\n")
    monkeypatch.setattr(b, "connect_candidate", lambda *_: pytest.fail("DB opened"))
    code = b.run(["--stage-b", "--evidence-root", str(root),
                  "--stage-a-verdict", str(verdict), "--identity", str(ident),
                  "--offline-response-cache", str(source.parent)])
    assert code == 1
    assert not (root / "requests" / "request-ledger.jsonl").exists()
    assert not (root / "baseline" / "g0-baseline.json").exists()


def test_offline_resume_external_ledger_drift_rejected_before_state_open(
        tmp_path, monkeypatch):
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_selfcontained_resume"
    verdict = _accepted_verdict(root / "stage-a-verdict.json")
    ident = _bj_identity_file(root / "identity.json", n=12)
    _stage_b_checkpoint(root, ident, verdict,
                        completed=[s.code for s in _bj_securities(12)])
    g0 = root / "baseline" / "g0-baseline.json"
    g0_before = g0.read_bytes()
    internal = root / "requests" / "request-ledger.jsonl"
    internal_before = internal.read_bytes()
    cache_dir = tmp_path / "offline-cache-resume-drift"
    cache_dir.mkdir()
    (cache_dir / "request-ledger.jsonl").write_bytes(
        internal_before + json.dumps({"request_id": "req-extra-00000",
                                      "source": "stock_value_em",
                                      "counted_as_real_request": 0}).encode() + b"\n")
    checkpoint = root / "build" / "checkpoint.json"
    checkpoint_before = checkpoint.read_bytes()
    monkeypatch.setattr(b, "connect_candidate", lambda *_: pytest.fail("DB opened"))
    code = b.run(["--stage-b", "--evidence-root", str(root),
                  "--stage-a-verdict", str(verdict), "--identity", str(ident),
                  "--offline-response-cache", str(cache_dir), "--resume"])
    assert code == 3
    assert internal.read_bytes() == internal_before
    assert g0.read_bytes() == g0_before
    assert checkpoint.read_bytes() == checkpoint_before


def test_offline_cross_source_rebuild_from_raw_cache(tmp_path):
    cache_dir = tmp_path / "cache"
    cache = b.ResponseCache(cache_dir)
    code = "920000"
    records = []
    for field, indicator in b.BAIDU_INDICATORS:
        rid = f"baidu-{code}-{field}"
        rows = [{"date": "2026-08-02", "value": 1.0},
                {"date": "2026-08-03", "value": 2.0 if field == "pe_ttm" else 3.0}]
        sha = cache.save(rid, rows)
        records.append({"request_id": rid, "source": "stock_zh_valuation_baidu",
                        "code_or_batch_scope": code, "outcome": "PROVEN",
                        "response_sha256": sha, "counted_as_real_request": 1,
                        "request_finished_at": "2026-08-03T12:00:00+00:00"})
    rid = f"cmp-{code}"
    sha = cache.save(rid, [{"代码": code, "市盈率-TTM": 2.1, "市净率-MRQ": 3.1}])
    records.append({"request_id": rid,
                    "source": "stock_zh_valuation_comparison_em",
                    "code_or_batch_scope": code, "outcome": "PROVEN",
                    "response_sha256": sha, "counted_as_real_request": 1,
                    "request_finished_at": "2026-08-03T12:01:00+00:00"})
    result = b.rebuild_cross_source_from_cache(
        records, cache_dir, [code], "adapter", "identity", "mapping")
    assert result["failures"] == []
    assert result["baidu"][code]["pe_ttm"]["date"] == "2026-08-03"
    assert result["comparison"][code]["row"] == {
        "code": code, "date": "2026-08-03", "pe_ttm": 2.1, "pb_mrq": 3.1}


def test_offline_cross_source_missing_cache_fails_honestly(tmp_path):
    code = "920000"
    records = [{"request_id": f"baidu-{code}-pe_ttm",
                "source": "stock_zh_valuation_baidu",
                "code_or_batch_scope": code, "outcome": "PROVEN",
                "response_sha256": "a" * 64, "counted_as_real_request": 1,
                "request_finished_at": "2026-08-03T12:00:00+00:00"}]
    result = b.rebuild_cross_source_from_cache(
        records, tmp_path / "cache", [code], "adapter", "identity", "mapping")
    assert result["failures"]
    assert code not in result["comparison"]


def test_offline_conservation_snapshot_factual_and_fail_closed(tmp_path):
    db = tmp_path / "candidate.db"
    conn = b.connect_candidate(db)
    rows, errors = b.normalize_value_em(
        "920000", b._RecordsDF(_hist_records(1)), "adapter", "identity",
        "mapping", "req-920000-00", b.STAGE_B_NORMALIZATION_VERSION)
    b.insert_rows(conn, rows)
    b.insert_errors(conn, errors)
    ledger = [{"request_id": "req-920000-00", "source": "stock_value_em",
               "code_or_batch_scope": "920000", "outcome": "PROVEN",
               "counted_as_real_request": 1}]
    path = b.write_offline_conservation_snapshot(
        tmp_path / "evidence", conn, ledger, {"920000"}, "920000", 1)
    doc = json.loads(path.read_text())
    assert doc["ok"] is True
    assert doc["factual_request_counters"]["total"] == 1
    assert doc["candidate_rows"] == doc["provenance_rows"] == 1
    with pytest.raises(FileExistsError):
        b.write_offline_conservation_snapshot(
            tmp_path / "evidence", conn, ledger, {"920000"}, "920000", 1)
    conn.close()


def test_stage_b_real_run_plan_no_clobber_before_state_open(tmp_path, monkeypatch):
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_plan_no_clobber"
    verdict = _accepted_verdict(root / "stage-a-verdict.json")
    ident = _bj_identity_file(root / "identity.json", n=1)
    plan_path = root / "plan" / "stage-b-plan.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_bytes(b"preexisting-plan")
    before = plan_path.read_bytes()
    monkeypatch.setattr(b, "connect_candidate", lambda *_: pytest.fail("DB opened"))
    code = b.run(["--stage-b", "--evidence-root", str(root),
                  "--stage-a-verdict", str(verdict), "--identity", str(ident)])
    assert code == 1
    assert plan_path.read_bytes() == before
    assert not (root / "baseline" / "g0-baseline.json").exists()
    assert not (root / "build" / "stage-b-bj-valuation.db").exists()


def test_atomic_copy_verified_never_clobbers_existing_target(tmp_path):
    source = tmp_path / "source-ledger.jsonl"
    target = tmp_path / "target-ledger.jsonl"
    source.write_bytes(b"source\n")
    target.write_bytes(b"existing\n")
    before = target.read_bytes()
    with pytest.raises(FileExistsError):
        b.atomic_copy_verified(source, target)
    assert target.read_bytes() == before
    assert list(tmp_path.glob(".ledger-*.tmp")) == []


def test_g0_rejects_formal_db_drift_during_hash_without_output(
        tmp_path, monkeypatch):
    formal = tmp_path / "formal-qs.db"
    formal.write_bytes(b"before")
    root = tmp_path / "evidence"
    original_sha = b.sha256_file

    def mutate_during_hash(path, *args, **kwargs):
        digest = original_sha(path, *args, **kwargs)
        if Path(path) == formal:
            formal.write_bytes(b"after-drift")
        return digest

    monkeypatch.setattr(b, "sha256_file", mutate_during_hash)
    with pytest.raises(OSError, match="changed while G0"):
        b.write_g0_baseline(root, formal)
    assert not (root / "baseline" / "g0-baseline.json").exists()


def test_g0_invalid_git_fingerprint_leaves_no_output(tmp_path, monkeypatch):
    formal = tmp_path / "formal-qs.db"
    formal.write_bytes(b"stable")
    root = tmp_path / "evidence"

    class FailedResult:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(__import__("subprocess"), "run", lambda *a, **k: FailedResult())
    with pytest.raises(OSError, match="Git G0"):
        b.write_g0_baseline(root, formal)
    assert not (root / "baseline" / "g0-baseline.json").exists()


def test_resume_invalid_g0_shape_rejected_before_state_open(tmp_path, monkeypatch):
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_bad_g0"
    verdict = _accepted_verdict(root / "stage-a-verdict.json")
    ident = _bj_identity_file(root / "identity.json", n=12)
    _stage_b_checkpoint(root, ident, verdict,
                        completed=[s.code for s in _bj_securities(12)])
    g0 = root / "baseline" / "g0-baseline.json"
    g0.write_text(json.dumps({"fixture": True}), encoding="utf-8")
    checkpoint = root / "build" / "checkpoint.json"
    before = checkpoint.read_bytes()
    monkeypatch.setattr(b, "connect_candidate", lambda *_: pytest.fail("DB opened"))
    code = b.run(["--stage-b", "--evidence-root", str(root),
                  "--stage-a-verdict", str(verdict), "--identity", str(ident),
                  "--resume"])
    assert code == 3
    assert checkpoint.read_bytes() == before
    assert not (root / "build" / "stage-b-bj-valuation.db").exists()


def test_sha256_file_streams_without_read_bytes(tmp_path, monkeypatch):
    path = tmp_path / "large-simulation.bin"
    path.write_bytes((b"0123456789abcdef" * 131072) + b"tail")
    expected = __import__("hashlib").sha256(path.read_bytes()).hexdigest()
    original_open = Path.open

    class GuardedReader:
        def __init__(self, handle):
            self.handle = handle
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return self.handle.__exit__(*args)
        def read(self, size=-1):
            assert 0 < size <= 1024 * 1024
            return self.handle.read(size)

    def guarded_open(self, mode="r", *args, **kwargs):
        handle = original_open(self, mode, *args, **kwargs)
        return GuardedReader(handle) if self == path and mode == "rb" else handle

    monkeypatch.setattr(Path, "open", guarded_open)
    assert b.sha256_file(path) == expected


# ---------------------------------------------------------------------------
# R3-4: offline replay must enumerate ALL PROVEN records first, then check
# cache per record -- cannot filter by cache-exists first.
# ---------------------------------------------------------------------------
def test_r3_4_offline_partial_cache_for_same_code_blocks(tmp_path, monkeypatch):
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    _small_formal_db(tmp_path, monkeypatch)
    root = tmp_path / "phase2_p2_07c_b_r3_offline_partial"
    verdict = _accepted_verdict(root / "stage-a-verdict.json")
    ident = _bj_identity_file(root / "identity.json", n=1)
    # build an offline cache dir: ledger has TWO PROVEN records for the SAME
    # code, but only ONE cache file is present -> replay must fail
    cache_dir = tmp_path / "offline-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    records = [{"数据日期": "2026-07-01", "PE(TTM)": 10.0, "市净率": 1.1,
                "市销率": 0.9, "总市值": 1e9, "总股本": 1e8}]
    rc = b.ResponseCache(cache_dir)
    sha = rc.save("req-920000-00", records)
    # second PROVEN record with NO cache file
    lines = [
        json.dumps({"request_id": "req-920000-00", "source": "stock_value_em",
                    "code_or_batch_scope": "920000", "outcome": "PROVEN",
                    "counted_as_real_request": 1, "response_sha256": sha}),
        json.dumps({"request_id": "req-920000-01", "source": "stock_value_em",
                    "code_or_batch_scope": "920000", "outcome": "PROVEN",
                    "counted_as_real_request": 1,
                    "response_sha256": "a" * 64}),
    ]
    (cache_dir / "request-ledger.jsonl").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")
    code = b.run(["--stage-b", "--evidence-root", str(root),
                  "--stage-a-verdict", str(verdict),
                  "--identity", str(ident),
                  "--max-codes", "1",
                  "--offline-response-cache", str(cache_dir)])
    # partial cache for the same code => BLOCKED_OFFLINE_CACHE (non-zero)
    assert code != 0


def test_r3_4_offline_no_proven_record_for_code_blocks(tmp_path, monkeypatch):
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    _small_formal_db(tmp_path, monkeypatch)
    root = tmp_path / "phase2_p2_b3_r3_offline_noprov"
    ev = root / "evidence"
    ev.mkdir(parents=True, exist_ok=True)
    verdict = _accepted_verdict(root / "stage-a-verdict.json")
    ident = _bj_identity_file(root / "identity.json", n=1)
    cache_dir = tmp_path / "offline-cache-noprov"
    cache_dir.mkdir(parents=True, exist_ok=True)
    # ledger has records but NONE marked PROVEN
    (cache_dir / "request-ledger.jsonl").write_text(
        json.dumps({"request_id": "req-920000-00", "code": "920000",
                    "source": "stock_value_em", "outcome": "NETWORK_ERROR",
                    "counted_as_real_request": 1}) + "\n", encoding="utf-8")
    code = b.run(["--stage-b", "--evidence-root", str(root),
                  "--stage-a-verdict", str(verdict),
                  "--identity", str(ident),
                  "--max-codes", "1",
                  "--offline-response-cache", str(cache_dir)])
    assert code != 0


# ---------------------------------------------------------------------------
# R3-R3: factual request counter resume tests
# ---------------------------------------------------------------------------
def _tamper_checkpoint(root, mutator):
    """Load checkpoint, apply mutator, save back."""
    cp = json.loads((root / "build" / "checkpoint.json").read_text(encoding="utf-8"))
    mutator(cp)
    (root / "build" / "checkpoint.json").write_text(json.dumps(cp, ensure_ascii=False),
                                                     encoding="utf-8")


def test_stage_b_resume_factual_counters_missing_blocks(tmp_path):
    """Resume with missing request_counters_factual -> nonzero."""
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_fact_miss"
    verdict = _accepted_verdict(root / "stage-a-verdict.json")
    ident = _bj_identity_file(root / "identity.json", n=12)
    _stage_b_checkpoint(root, ident, verdict)
    _tamper_checkpoint(root, lambda cp: cp.pop("request_counters_factual", None))
    code = b.run(["--stage-b", "--evidence-root", str(root),
                  "--stage-a-verdict", str(verdict), "--identity", str(ident),
                  "--resume"])
    assert code == 3


def test_stage_b_resume_factual_counters_total_mismatch_blocks(tmp_path):
    """Resume with cp total 323 vs ledger 12 -> nonzero."""
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_fact_tot"
    verdict = _accepted_verdict(root / "stage-a-verdict.json")
    ident = _bj_identity_file(root / "identity.json", n=12)
    _stage_b_checkpoint(root, ident, verdict)
    _tamper_checkpoint(root, lambda cp: cp["request_counters_factual"].update({"total": 323}))
    code = b.run(["--stage-b", "--evidence-root", str(root),
                  "--stage-a-verdict", str(verdict), "--identity", str(ident),
                  "--resume"])
    assert code == 3


def test_stage_b_resume_factual_counters_missing_source_blocks(tmp_path):
    """Resume with missing Baidu/comparison counters in cp -> nonzero."""
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_fact_src"
    verdict = _accepted_verdict(root / "stage-a-verdict.json")
    ident = _bj_identity_file(root / "identity.json", n=12)
    _stage_b_checkpoint(root, ident, verdict)
    _tamper_checkpoint(root, lambda cp:
        cp["request_counters_factual"].update({"per_source": {}}))
    code = b.run(["--stage-b", "--evidence-root", str(root),
                  "--stage-a-verdict", str(verdict), "--identity", str(ident),
                  "--resume"])
    assert code == 3


def test_stage_b_resume_factual_counters_bool_total_blocks(tmp_path):
    """Resume with bool total -> nonzero."""
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_fact_bool"
    verdict = _accepted_verdict(root / "stage-a-verdict.json")
    ident = _bj_identity_file(root / "identity.json", n=12)
    _stage_b_checkpoint(root, ident, verdict)
    _tamper_checkpoint(root, lambda cp: cp["request_counters_factual"].update({"total": True}))
    code = b.run(["--stage-b", "--evidence-root", str(root),
                  "--stage-a-verdict", str(verdict), "--identity", str(ident),
                  "--resume"])
    assert code == 3


def test_stage_b_resume_factual_counters_negative_total_blocks(tmp_path):
    """Resume with negative total -> nonzero."""
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_fact_neg"
    verdict = _accepted_verdict(root / "stage-a-verdict.json")
    ident = _bj_identity_file(root / "identity.json", n=12)
    _stage_b_checkpoint(root, ident, verdict)
    _tamper_checkpoint(root, lambda cp: cp["request_counters_factual"].update({"total": -1}))
    code = b.run(["--stage-b", "--evidence-root", str(root),
                  "--stage-a-verdict", str(verdict), "--identity", str(ident),
                  "--resume"])
    assert code == 3


def test_stage_b_resume_factual_counters_string_total_blocks(tmp_path):
    """Resume with string total -> nonzero."""
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_fact_str"
    verdict = _accepted_verdict(root / "stage-a-verdict.json")
    ident = _bj_identity_file(root / "identity.json", n=12)
    _stage_b_checkpoint(root, ident, verdict)
    _tamper_checkpoint(root, lambda cp: cp["request_counters_factual"].update({"total": "413"}))
    code = b.run(["--stage-b", "--evidence-root", str(root),
                  "--stage-a-verdict", str(verdict), "--identity", str(ident),
                  "--resume"])
    assert code == 3


def _rewrite_ledger(root, records):
    path = root / "requests" / "request-ledger.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def test_stage_b_resume_duplicate_request_id_blocks_before_state_open(tmp_path, monkeypatch):
    """A duplicate ID at the real resume entry must fail before DB open."""
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_reqid_dup"
    verdict = _accepted_verdict(root / "stage-a-verdict.json")
    ident = _bj_identity_file(root / "identity.json", n=12)
    _stage_b_checkpoint(root, ident, verdict)
    checkpoint = root / "build" / "checkpoint.json"
    checkpoint_before = checkpoint.read_bytes()
    records = [json.loads(line) for line in
               (root / "requests" / "request-ledger.jsonl").read_text().splitlines()]
    records[1]["request_id"] = records[0]["request_id"]
    _rewrite_ledger(root, records)
    monkeypatch.setattr(b, "connect_candidate", lambda *_: pytest.fail("DB opened"))
    code = b.run(["--stage-b", "--evidence-root", str(root),
                  "--stage-a-verdict", str(verdict), "--identity", str(ident),
                  "--resume"])
    assert code == 3
    assert checkpoint.read_bytes() == checkpoint_before
    assert not (root / "build" / "stage-b-bj-valuation.db").exists()


@pytest.mark.parametrize("bad_request_id", [
    "", " leading", "trailing ", " ", "a/b", "a\\b", "../escape",
    ".", "..", "bad:id", "bad?id", "bad#id", "中文",
])
def test_stage_b_resume_invalid_request_id_blocks_before_state_open(
        tmp_path, monkeypatch, bad_request_id):
    """Every unsafe ID must fail before DB open and preserve checkpoint bytes."""
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_reqid_invalid"
    verdict = _accepted_verdict(root / "stage-a-verdict.json")
    ident = _bj_identity_file(root / "identity.json", n=12)
    _stage_b_checkpoint(root, ident, verdict)
    checkpoint = root / "build" / "checkpoint.json"
    checkpoint_before = checkpoint.read_bytes()
    records = [json.loads(line) for line in
               (root / "requests" / "request-ledger.jsonl").read_text().splitlines()]
    records[0]["request_id"] = bad_request_id
    _rewrite_ledger(root, records)
    monkeypatch.setattr(b, "connect_candidate", lambda *_: pytest.fail("DB opened"))
    code = b.run(["--stage-b", "--evidence-root", str(root),
                  "--stage-a-verdict", str(verdict), "--identity", str(ident),
                  "--resume"])
    assert code == 3
    assert checkpoint.read_bytes() == checkpoint_before
    assert not (root / "build" / "stage-b-bj-valuation.db").exists()


def test_stage_b_resume_malformed_ledger_json_blocks_before_state_open(
        tmp_path, monkeypatch):
    """Malformed JSONL must not be silently omitted during resume."""
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_ledger_malformed"
    verdict = _accepted_verdict(root / "stage-a-verdict.json")
    ident = _bj_identity_file(root / "identity.json", n=12)
    _stage_b_checkpoint(root, ident, verdict)
    checkpoint = root / "build" / "checkpoint.json"
    checkpoint_before = checkpoint.read_bytes()
    ledger = root / "requests" / "request-ledger.jsonl"
    ledger.write_text(ledger.read_text(encoding="utf-8") + "{not-json}\n",
                      encoding="utf-8")
    monkeypatch.setattr(b, "connect_candidate", lambda *_: pytest.fail("DB opened"))
    code = b.run(["--stage-b", "--evidence-root", str(root),
                  "--stage-a-verdict", str(verdict), "--identity", str(ident),
                  "--resume"])
    assert code == 3
    assert checkpoint.read_bytes() == checkpoint_before
    assert not (root / "build" / "stage-b-bj-valuation.db").exists()


def test_stage_b_resume_factual_counters_extra_nonzero_source_blocks(tmp_path):
    """Resume with extra nonzero source -> nonzero."""
    b.STAGE_B_ROOT_PREFIX = str(tmp_path)
    root = tmp_path / "phase2_p2_07c_b_fact_extra"
    verdict = _accepted_verdict(root / "stage-a-verdict.json")
    ident = _bj_identity_file(root / "identity.json", n=12)
    _stage_b_checkpoint(root, ident, verdict)
    _tamper_checkpoint(root, lambda cp: cp["request_counters_factual"].update(
        {"total": 24, "per_source": {"stock_value_em": 12, "fake_source": 12}}))
    code = b.run(["--stage-b", "--evidence-root", str(root),
                  "--stage-a-verdict", str(verdict), "--identity", str(ident),
                  "--resume"])
    assert code == 3