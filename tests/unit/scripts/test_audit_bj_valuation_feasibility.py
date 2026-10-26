"""P2-07B-R1 audit regression tests."""

import json

from scripts.audit_bj_valuation_feasibility import (
    GO_GATES,
    TARGET_FIELDS,
    VERDICT_GO,
    VERDICT_NO_GO,
    VERDICT_PARTIAL,
    analyze,
    main as audit_main,
)


APPROVED_SOURCES = [
    "stock_value_em",
    "stock_bj_a_spot_em",
    "stock_zh_valuation_baidu",
    "stock_zh_valuation_comparison_em",
]


def _mk_mapping(status="CANDIDATE_NAME_MATCH", old="", new="920001", usable_mig=False):
    official = status == "OFFICIAL_PROVEN"
    return {
        "old_code": old,
        "new_code": new,
        "name_old": "x" if old else "",
        "name_new": "x",
        "mapping_status": status,
        "confidence": "high" if official else "medium" if status == "CANDIDATE_NAME_MATCH" else "low",
        "evidence_type": "bse_official_mapping_attachment" if official else "none_local",
        "evidence_url": "https://www.bse.cn/uploads/mapping.docx" if official else "",
        "evidence_date": "2026-08-02T20:00:00+08:00" if official else "",
        "evidence_sha256": "e" * 64 if official else "",
        "evidence_excerpt": "x 2020-01-01 old new" if official else "",
        "conflicts": [],
        "usable_for_probe": True,
        "usable_for_migration": usable_mig,
        "reason": "test",
        "name": "x",
        "exchange": "BJ",
        "effective_date": "2020-01-01",
        "evidence_source": "Beijing Stock Exchange" if official else status,
        "one_to_one": True,
    }


def _mk_result(
    code="920001", field="pe_ttm", source="stock_value_em",
    status="PROVEN", hist=True, asof="2024-05-01", unit="倍",
    sha="s1", request_id="r1", observation_id="o1",
    err="", errcls="", code_type="new", value=20.0,
):
    return {
        "observation_id": observation_id,
        "request_id": request_id,
        "code_requested": code,
        "code_resolved": code,
        "code_type": code_type,
        "name": "x",
        "source": source,
        "field": field,
        "value": value if status == "PROVEN" else None,
        "unit": unit,
        "as_of_date": asof,
        "period_start": "2023-05-01",
        "period_end": "2025-06-30",
        "is_historical_series": hist,
        "source_column": "PE(TTM)",
        "source_title_or_endpoint": "test",
        "response_sha256": sha,
        "retrieved_at": "2026-08-02T00:00:00Z",
        "status": status,
        "error": err,
        "error_class": errcls,
        "latency_ms": 1.0,
    }


def _official_mappings():
    return [
        _mk_mapping("OFFICIAL_PROVEN", "830799", "920799", True),
        _mk_mapping("OFFICIAL_PROVEN", "835185", "920185", True),
        _mk_mapping("OFFICIAL_PROVEN", "430017", "920017", True),
    ]


def _candidate_mappings():
    return [
        _mk_mapping("CANDIDATE_NAME_MATCH", "830799", "920799"),
        _mk_mapping("CANDIDATE_NAME_MATCH", "835185", "920185"),
        _mk_mapping("CANDIDATE_NAME_MATCH", "430017", "920017"),
    ]


def _history_results(codes=("920799", "920185", "920017")):
    results = []
    for ci, code in enumerate(codes, 1):
        request_id = f"history-{ci}"
        oi = 0
        for field in ("pe_ttm", "pb_mrq"):
            for asof in ("2023-05-01", "2024-05-01", "2025-06-30"):
                oi += 1
                results.append(_mk_result(
                    code=code, field=field, asof=asof,
                    request_id=request_id,
                    observation_id=f"obs-{ci}-{oi}",
                    sha=f"sha-{ci}",
                ))
    return results


def _plan(codes=("920799", "920185", "920017")):
    return [
        {"code": c, "code_type": "new", "name": "x", "source": "", "field": "", "reason": "r"}
        for c in codes
    ]


def _ledger(codes=("920799", "920185", "920017")):
    return [
        {
            "request_id": f"history-{i}",
            "source": "stock_value_em",
            "endpoint": "test",
            "attempt_no": 1,
            "is_retry": False,
            "code_or_batch_scope": code,
            "outcome": "PROVEN",
            "counted_as_real_request": True,
        }
        for i, code in enumerate(codes, 1)
    ]


def _consistency(codes=("920799", "920185", "920017"), rel=0.001):
    return [
        {
            "code": code,
            "field": "pe_ttm",
            "comparison_status": "SAMPLE_ALIGNED" if rel <= 0.02 else "SAMPLE_DIFFERENT",
            "sample_consistent_le_2pct": rel <= 0.02,
            "relative_difference": rel,
        }
        for code in codes
    ]


def _execution(codes=("920799", "920185", "920017")):
    count = len(codes)
    return {
        "runtime_real_request_count": count,
        "runtime_requests_by_source": {"stock_value_em": count},
        "wall_clock_within_20_minutes": True,
        "approved_free_sources": APPROVED_SOURCES,
        "new_credentials_used": False,
        "checkpoint_resume_test_passed": True,
        "nonzero_error_propagation_test_passed": True,
    }


def _analyze_good(**overrides):
    args = {
        "mapping": _official_mappings(),
        "results": _history_results(),
        "plan": _plan(),
        "consistency": _consistency(),
        "request_ledger": _ledger(),
        "execution_evidence": _execution(),
    }
    args.update(overrides)
    return analyze(**args)


def test_go_requires_all_explicit_evidence():
    report = _analyze_good()
    assert report["verdict"] == VERDICT_GO
    assert report["all_gates_pass"] is True
    assert len(GO_GATES) == 13
    assert all(isinstance(g["passed"], bool) and g.get("evidence") for g in report["verdict_gates"].values())


def test_no_official_mapping_is_partial():
    report = _analyze_good(mapping=_candidate_mappings())
    assert report["verdict"] == VERDICT_PARTIAL
    assert not report["verdict_gates"]["migration_mapping_not_solely_same_name"]["passed"]


def test_incomplete_official_mapping_does_not_count():
    mappings = _official_mappings()
    mappings[0]["evidence_sha256"] = ""
    report = _analyze_good(mapping=mappings)
    assert report["mapping_distribution"]["official_proven"] == 2
    assert report["verdict"] != VERDICT_GO


def test_no_history_is_no_go():
    results = [_mk_result(status="NETWORK_ERROR", hist=False, request_id="history-1")]
    report = _analyze_good(results=results)
    assert report["verdict"] == VERDICT_NO_GO


def test_pe_pb_must_overlap_in_one_history_source():
    results = _history_results()
    for row in results:
        if row["field"] == "pb_mrq":
            row["source"] = "other_history_source"
    ledger = _ledger()
    ledger.extend([
        {**row, "request_id": row["request_id"].replace("history", "other"),
         "source": "other_history_source"}
        for row in _ledger()
    ])
    for row in results:
        if row["field"] == "pb_mrq":
            row["request_id"] = row["request_id"].replace("history", "other")
    execution = _execution()
    execution["runtime_real_request_count"] = len(ledger)
    execution["runtime_requests_by_source"] = {
        "stock_value_em": 3,
        "other_history_source": 3,
    }
    execution["approved_free_sources"].append("other_history_source")
    report = _analyze_good(
        results=results, request_ledger=ledger, execution_evidence=execution,
    )
    assert not report["verdict_gates"]["real_history_pe_pb_from_one_source"]["passed"]
    assert report["verdict"] == VERDICT_NO_GO


def test_one_security_insufficient_for_go():
    codes = ("920799",)
    report = _analyze_good(
        results=_history_results(codes), plan=_plan(codes),
        request_ledger=_ledger(codes), consistency=_consistency(codes),
        execution_evidence=_execution(codes),
    )
    assert report["verdict"] != VERDICT_GO


def test_schema_drift_and_silent_empty_each_fail_gate():
    for err, errcls in (("column missing", "schema_drift"), ("empty dataframe", "")):
        results = _history_results()
        results.append(_mk_result(
            status="FIELD_UNAVAILABLE", hist=False, asof="",
            request_id="history-1", observation_id=f"bad-{err}",
            err=err, errcls=errcls,
        ))
        report = _analyze_good(results=results)
        assert not report["verdict_gates"]["no_schema_drift_or_silent_empty"]["passed"]
        assert report["verdict"] != VERDICT_GO


def test_missing_retrieved_at_fails_provenance():
    results = _history_results()
    results[0]["retrieved_at"] = ""
    report = _analyze_good(results=results)
    assert not report["verdict_gates"]["per_record_provenance_sha"]["passed"]


def test_request_ledger_mismatch_fails():
    execution = _execution()
    execution["runtime_real_request_count"] = 99
    report = _analyze_good(execution_evidence=execution)
    assert not report["verdict_gates"]["request_ledger_conserved"]["passed"]
    assert report["verdict"] != VERDICT_GO


def test_missing_observation_request_reference_fails():
    results = _history_results()
    results[0]["request_id"] = "missing"
    report = _analyze_good(results=results)
    assert report["probe_stats"]["missing_observation_request_refs"] == ["missing"]
    assert not report["verdict_gates"]["request_ledger_conserved"]["passed"]


def test_zero_or_two_cross_source_securities_not_go():
    for consistency in ([], _consistency(("920799", "920185"))):
        report = _analyze_good(consistency=consistency)
        assert not report["verdict_gates"]["cross_source_three_securities"]["passed"]
        assert report["verdict"] != VERDICT_GO


def test_cross_source_difference_enters_strongest_objection():
    report = _analyze_good(consistency=_consistency(rel=0.10))
    assert not report["verdict_gates"]["strongest_objection_not_fatal"]["passed"]


def test_credentials_and_checkpoint_are_not_unconditional():
    execution = _execution()
    execution["approved_free_sources"] = []
    execution["checkpoint_resume_test_passed"] = False
    report = _analyze_good(execution_evidence=execution)
    assert not report["verdict_gates"]["no_paid_or_new_credentials"]["passed"]
    assert not report["verdict_gates"]["failures_checkpoint_resume"]["passed"]


def test_probe_budget_is_separate_from_full_build_estimate():
    report = _analyze_good()
    assert "probe_request_budget_within_limits" in report["verdict_gates"]
    assert "estimated_full_build_bounded" in report["verdict_gates"]
    assert report["verdict_gates"]["probe_request_budget_within_limits"]["passed"]


def test_field_counts_conserve():
    report = _analyze_good()
    total = sum(sum(report["field_status"][f].values()) for f in TARGET_FIELDS)
    assert total == len(_history_results())
    hist_total = sum(v["samples"] for v in report["history"]["date_ranges"].values())
    assert hist_total == 18


def _write_cli_inputs(tmp_path):
    (tmp_path / "bj-code-mapping.json").write_text(json.dumps(_official_mappings()), encoding="utf-8")
    (tmp_path / "probe-results.json").write_text(json.dumps(_history_results()), encoding="utf-8")
    (tmp_path / "probe-plan.json").write_text(json.dumps(_plan()), encoding="utf-8")
    (tmp_path / "cross-source-consistency.json").write_text(json.dumps(_consistency()), encoding="utf-8")
    (tmp_path / "request-ledger.json").write_text(json.dumps(_ledger()), encoding="utf-8")
    (tmp_path / "execution-evidence.json").write_text(json.dumps(_execution()), encoding="utf-8")


def test_missing_input_dir_nonzero(tmp_path):
    assert audit_main(["--input-dir", str(tmp_path / "nope"), "--output-dir", str(tmp_path)]) == 1


def test_missing_request_ledger_nonzero(tmp_path):
    _write_cli_inputs(tmp_path)
    (tmp_path / "request-ledger.json").unlink()
    assert audit_main(["--input-dir", str(tmp_path), "--output-dir", str(tmp_path / "out")]) == 1


def test_successful_run_exit_zero(tmp_path):
    _write_cli_inputs(tmp_path)
    out = tmp_path / "analysis"
    assert audit_main(["--input-dir", str(tmp_path), "--output-dir", str(out)]) == 0
    verdict = json.loads((out / "verdict.json").read_text(encoding="utf-8"))
    assert verdict["verdict"] == VERDICT_GO
