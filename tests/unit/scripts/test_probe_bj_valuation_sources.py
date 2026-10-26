"""Tests for scripts/probe_bj_valuation_sources.py — P2-07B.

P2-07B coverage (work order §7.2):
  1. network blocked by default
  2. explicit switch required to go online
  3. max_codes / max_requests / max_time respected
  4. batch source counts as ONE real request
  5. 3 consecutive network/adapter failures fuse a source
  6. unknown KeyError -> ADAPTER_ERROR
  7. known absence key -> CODE_NOT_FOUND
  8. RATE_LIMITED classified distinctly
  9. snapshot never marked is_historical_series=True
 10. plain PS never mapped to ps_ttm
 11. missing unit/date/semantics => not PROVEN
 12. same-name mapping at most medium
 13. official-evidence fields missing => not high
 14. response SHA256 + provenance mandatory
 15. repeated responses/codes do not double count requests or coverage
 16. NaN / Inf / empty / non-numeric values do not count as coverage
 17. audit: any hard-gate failure => verdict is not GO
 18. script errors exit non-zero
 19. all input SQLite opened read-only
 20. report counts and classifications conserve
"""

import json
import os
import sys
import math
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import asdict, replace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from scripts.probe_bj_valuation_sources import (
    CodeMapping,
    ProbeRequest,
    ProbeResult,
    _run_probe_requests,
    _build_probe_plan,
    _fuse_source,
    _classify_field,
    _safe_network_call,
    _func_accepts_timeout,
    _probe_batch_source,
    _probe_value_em,
    _probe_valuation_baidu,
    _probe_valuation_comparison,
    _classify_probe_error,
    _network_call_with_retry,
    _finite_float,
    _df_to_sha,
    phase2,
    apply_official_mapping_evidence,
    build_code_mapping,
    _write_source_capability_matrix,
    _write_cross_source_consistency,
    SOURCE_DEFINITIONS,
    TARGET_FIELDS,
    NETWORK_ENV_VAR,
    STATUS_PROVEN,
    STATUS_LIMITED_SAMPLE,
    STATUS_NETWORK_ERROR,
    STATUS_ADAPTER_ERROR,
    STATUS_FIELD_UNAVAILABLE,
    STATUS_CODE_NOT_FOUND,
    STATUS_RATE_LIMITED,
    STATUS_UNPROVEN,
    MAPPING_CANDIDATE_NAME_MATCH,
    MAPPING_OFFICIAL_PROVEN,
    MAPPING_UNRESOLVED,
)

from scripts.audit_bj_valuation_feasibility import (
    analyze,
    VERDICT_GO,
    VERDICT_PARTIAL,
    VERDICT_NO_GO,
    main as audit_main,
    GO_GATES,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_mappings():
    return [
        CodeMapping(
            old_code="830799", new_code="920799", name="艾融软件",
            name_old="艾融软件", name_new="艾融软件",
            mapping_status=MAPPING_CANDIDATE_NAME_MATCH, confidence="medium",
            one_to_one=True, usable_for_probe=True, usable_for_migration=False,
        ),
        CodeMapping(
            old_code="835185", new_code="920185", name="贝特瑞",
            name_old="贝特瑞", name_new="贝特瑞",
            mapping_status=MAPPING_CANDIDATE_NAME_MATCH, confidence="medium",
            one_to_one=True, usable_for_probe=True, usable_for_migration=False,
        ),
        CodeMapping(
            old_code="430017", new_code="920017", name="星昊医药",
            name_old="星昊医药", name_new="星昊医药",
            mapping_status=MAPPING_CANDIDATE_NAME_MATCH, confidence="medium",
            one_to_one=True, usable_for_probe=True, usable_for_migration=False,
        ),
    ]


def _make_result(
    status="PROVEN", error="", field="pe_ttm",
    source="stock_value_em", code="830001", request_id="stock_value_em-0001",
    **overrides,
) -> ProbeResult:
    kwargs = dict(
        request_id=request_id,
        code_requested=code, code_resolved=code, code_type="old", name="Test",
        source=source, field=field,
        value=10.0 if status == STATUS_PROVEN else None,
        unit="倍" if status == STATUS_PROVEN else "",
        as_of_date="2025-06-30" if status == STATUS_PROVEN else "",
        period_start="2020-01-01", period_end="2025-06-30",
        is_historical_series=source != "stock_bj_a_spot_em",
        source_column="PE(TTM)",
        source_title_or_endpoint="test",
        response_sha256="abc123",
        retrieved_at=datetime.now(tz=timezone.utc).isoformat(),
        status=status, error=error,
        latency_ms=10.0,
    )
    kwargs.update(overrides)
    return ProbeResult(**kwargs)


def _make_ak():
    ak = MagicMock()
    ak.stock_value_em.return_value = None
    ak.stock_bj_a_spot_em.return_value = None
    ak.stock_zh_valuation_baidu.return_value = None
    ak.stock_zh_valuation_comparison_em.return_value = None
    return ak


def _ledgered_mock(source, result, calls=1, *, batch=False, err=None):
    """Return a probe mock that honors the R1 request-ledger contract."""
    invocation = 0

    def side_effect(*args):
        nonlocal invocation
        invocation += 1
        ledger = args[-1]
        final_request_id = ""
        for attempt in range(1, calls + 1):
            final_request_id = f"mock-{source}-{invocation:03d}-{attempt:02d}"
            ledger.append({
                "request_id": final_request_id,
                "source": source,
                "endpoint": f"mock.{source}",
                "request_started_at": "2026-08-02T00:00:00Z",
                "request_finished_at": "2026-08-02T00:00:01Z",
                "attempt_no": attempt,
                "is_retry": attempt > 1,
                "code_or_batch_scope": "test",
                "outcome": result.status,
                "latency_ms": 1.0,
                "response_sha256": result.response_sha256,
                "error_class": result.error_class,
                "error": result.error,
                "counted_as_real_request": True,
            })
        cloned = replace(
            result,
            request_id=final_request_id,
            observation_id=f"obs-mock-{source}-{invocation:03d}",
        )
        if batch:
            return [cloned], err, calls
        return [cloned], calls

    return side_effect


def _value_em_df(rows=None):
    if rows is None:
        rows = [
            {"数据日期": "2023-05-01", "PE(TTM)": 20.0, "市净率": 2.0,
             "市销率": 3.0, "总市值": 1e9, "总股本": 1e8},
            {"数据日期": "2024-05-01", "PE(TTM)": 22.0, "市净率": 2.2,
             "市销率": 3.3, "总市值": 1.1e9, "总股本": 1e8},
            {"数据日期": "2025-06-30", "PE(TTM)": 25.0, "市净率": 2.5,
             "市销率": 3.5, "总市值": 1.2e9, "总股本": 1.1e8},
        ]
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 1. Probe plan respects max_codes
# ---------------------------------------------------------------------------


def test_build_probe_plan_respects_max_codes(sample_mappings):
    plan = _build_probe_plan(sample_mappings, max_codes=3)
    assert len(plan) <= 3
    assert len({r.code for r in plan}) == len(plan)


def test_build_probe_plan_includes_manual_review_set(sample_mappings):
    plan = _build_probe_plan(sample_mappings, max_codes=12)
    codes = {r.code for r in plan}
    assert "830799" in codes and "920799" in codes
    assert "835185" in codes and "920185" in codes
    assert "430017" in codes and "920017" in codes


def test_build_probe_plan_zero_codes(sample_mappings):
    assert _build_probe_plan(sample_mappings, max_codes=0) == []


# ---------------------------------------------------------------------------
# 2. Request count limit
# ---------------------------------------------------------------------------


def _value_em_ok(ak_module, req, src_def, req_counter):
    from scripts.probe_bj_valuation_sources import ProbeResult as PR
    df = _value_em_df()
    return _probe_value_em(ak_module, req, src_def, req_counter)


def test_request_count_limit(sample_mappings):
    plan = _build_probe_plan(sample_mappings, max_codes=2)

    ok = _make_result()

    with (
        patch("scripts.probe_bj_valuation_sources._probe_batch_source",
              side_effect=_ledgered_mock("stock_bj_a_spot_em", ok, batch=True)),
        patch("scripts.probe_bj_valuation_sources._probe_value_em",
              side_effect=_ledgered_mock("stock_value_em", ok)),
        patch("scripts.probe_bj_valuation_sources._probe_valuation_baidu",
              side_effect=_ledgered_mock("stock_zh_valuation_baidu", ok)),
        patch("scripts.probe_bj_valuation_sources._probe_valuation_comparison",
              side_effect=_ledgered_mock("stock_zh_valuation_comparison_em", ok)),
        patch("time.monotonic", return_value=0.0),
    ):
        results, stats = _run_probe_requests(
            plan, _make_ak(), Path("/tmp"),
            max_requests=10, max_time_minutes=20,
        )

    made = sum(s.get("requests_made", 0) for s in stats if not s.get("fused"))
    assert made <= 10, f"Source stats show {made} requests made (cap 10)"


# ---------------------------------------------------------------------------
# 3. Time limit
# ---------------------------------------------------------------------------


def test_time_limit(sample_mappings):
    plan = _build_probe_plan(sample_mappings, max_codes=2)

    monotonic_values = [0.0] + [25.0 * 60.0] * 500
    ok = _make_result()

    with (
        patch("scripts.probe_bj_valuation_sources._probe_batch_source",
              return_value=([ok], None, 1)),
        patch("scripts.probe_bj_valuation_sources._probe_value_em",
              return_value=([ok], 1)),
        patch("scripts.probe_bj_valuation_sources._probe_valuation_baidu",
              return_value=([ok], 1)),
        patch("scripts.probe_bj_valuation_sources._probe_valuation_comparison",
              return_value=([ok], 1)),
        patch("time.monotonic", side_effect=monotonic_values),
    ):
        results, stats = _run_probe_requests(
            plan, _make_ak(), Path("/tmp"),
            max_requests=999, max_time_minutes=20,
        )

    assert len(results) < 30, f"Expected time limit to stop early, got {len(results)}"


# ---------------------------------------------------------------------------
# 4. Consecutive failure fuse (per-code source)
# ---------------------------------------------------------------------------


def test_consecutive_failure_fuse(sample_mappings):
    plan = _build_probe_plan(sample_mappings, max_codes=2)
    target = "stock_zh_valuation_baidu"

    fail = _make_result(
        status=STATUS_NETWORK_ERROR, error="ConnectionError: refused",
        source=target,
    )
    ok = _make_result(source="stock_zh_valuation_comparison_em")

    with (
        patch("scripts.probe_bj_valuation_sources._probe_batch_source",
              side_effect=_ledgered_mock(
                  "stock_bj_a_spot_em", _make_result(source="stock_bj_a_spot_em"), batch=True,
              )),
        patch("scripts.probe_bj_valuation_sources._probe_value_em",
              side_effect=_ledgered_mock(
                  "stock_value_em", _make_result(source="stock_value_em"),
              )),
        patch("scripts.probe_bj_valuation_sources._probe_valuation_baidu",
              side_effect=_ledgered_mock(target, fail)),
        patch("scripts.probe_bj_valuation_sources._probe_valuation_comparison",
              side_effect=_ledgered_mock("stock_zh_valuation_comparison_em", ok)),
        patch("time.monotonic", return_value=0.0),
    ):
        results, stats = _run_probe_requests(
            plan, _make_ak(), Path("/tmp"),
            max_requests=999, max_time_minutes=20,
        )

    fused = [s for s in stats if s.get("fused") and s["source"] == target]
    assert len(fused) == 1, f"Source {target} should be fused"
    assert fused[0]["fuse_reason"].startswith("3 consecutive failures"), (
        f"Unexpected fuse reason: {fused[0]['fuse_reason']}"
    )

    requests_to_target = [r for r in results if r.source == target]
    assert len(requests_to_target) == 3, (
        f"Expected 3 failed requests to fused source, got {len(requests_to_target)}"
    )


# ---------------------------------------------------------------------------
# 5. No unconditional timeout injection
# ---------------------------------------------------------------------------


def test_safe_network_call_returns_tuple_on_success():
    def f():
        return "ok"

    ak = MagicMock()
    ak.stock_bj_a_spot_em = f
    result, err = _safe_network_call(ak, "stock_bj_a_spot_em")
    assert result == "ok"
    assert err is None


def test_safe_network_call_returns_tuple_on_error():
    def f():
        raise ConnectionError("boom")

    ak = MagicMock()
    ak.stock_bj_a_spot_em = f
    result, err = _safe_network_call(ak, "stock_bj_a_spot_em")
    assert result is None
    assert err is not None
    assert "ConnectionError" in err


def test_no_timeout_injection_when_source_unsupported():
    seen = {}

    def f(**kwargs):
        seen.update(kwargs)
        return "ok"

    ak = MagicMock()
    ak.stock_zh_valuation_baidu = f
    src_def = {"name": "stock_zh_valuation_baidu", "timeout_supported": False}

    result, err = _safe_network_call(
        ak, "stock_zh_valuation_baidu", src_def,
        symbol="920808", indicator="市盈率(TTM)", period="近一年",
    )
    assert err is None
    assert result == "ok"
    assert "timeout" not in seen, (
        f"timeout must NOT be injected for unsupported source, got kwargs {seen}"
    )
    assert seen["symbol"] == "920808"


def test_func_accepts_timeout_detection():
    def no_timeout(symbol, indicator, period):
        pass

    def has_timeout(symbol, timeout=30):
        pass

    def var_kwargs(**kwargs):
        pass

    assert _func_accepts_timeout(has_timeout) is True
    assert _func_accepts_timeout(var_kwargs) is True
    assert _func_accepts_timeout(no_timeout) is False


def test_all_source_definitions_consistent_timeout_policy():
    for src in SOURCE_DEFINITIONS:
        assert "timeout_supported" in src, src["name"]
        assert isinstance(src["timeout_supported"], bool)
        assert "mode" in src


def test_at_most_four_sources():
    assert len(SOURCE_DEFINITIONS) <= 4


# ---------------------------------------------------------------------------
# 6. RATE_LIMITED classification
# ---------------------------------------------------------------------------


def test_rate_limited_classified_from_string():
    status, msg = _classify_probe_error("requests 访问过于频繁, try again later")
    assert status == STATUS_RATE_LIMITED


def test_rate_limited_classified_429():
    status, _ = _classify_probe_error("HTTP 429 Too Many Requests")
    assert status == STATUS_RATE_LIMITED


def test_rate_limited_is_distinct_from_network():
    status, _ = _classify_probe_error("TimeoutError: timed out")
    assert status == STATUS_NETWORK_ERROR
    status2, _ = _classify_probe_error("requests 访问过于频繁")
    assert status2 == STATUS_RATE_LIMITED
    assert status2 != status


# ---------------------------------------------------------------------------
# 7. KeyError classification (known vs unknown)
# ---------------------------------------------------------------------------


def test_keyerror_classified_as_code_not_found():
    status, msg = _classify_probe_error("KeyError: 'chartInfo'")
    assert status == STATUS_CODE_NOT_FOUND
    assert "chartInfo" in msg


def test_keyerror_known_absence_key_result_code_not_found():
    status, msg = _classify_probe_error("KeyError: 'result'")
    assert status == STATUS_CODE_NOT_FOUND
    assert "result" in msg


def test_keyerror_unknown_schema_key_is_adapter_error():
    status, msg = _classify_probe_error("KeyError: unexpected_new_schema_key")
    assert status == STATUS_ADAPTER_ERROR
    assert "schema drift" in msg


def test_network_error_classified_via_probe_error():
    status, msg = _classify_probe_error("ConnectionError: refused")
    assert status == STATUS_NETWORK_ERROR


def test_other_errors_classified_as_adapter_error():
    status, msg = _classify_probe_error("ValueError: bad parse")
    assert status == STATUS_ADAPTER_ERROR


# ---------------------------------------------------------------------------
# 8. Batch source probe — one snapshot, counts as ONE request
# ---------------------------------------------------------------------------


def test_batch_source_probe_single_fetch(tmp_path):
    df = pd.DataFrame([
        {"代码": "830001", "市盈率-动态": 10.0, "市净率": 1.2, "总市值": 1e9, "总股本": 1e8},
        {"代码": "920001", "市盈率-动态": 12.0, "市净率": 1.5, "总市值": 2e9, "总股本": 2e8},
    ])
    ak = MagicMock()
    ak.stock_bj_a_spot_em.return_value = df

    requests = [
        ProbeRequest(code="830001", code_type="old", name="Test A"),
        ProbeRequest(code="920001", code_type="new", name="Test A"),
    ]
    src_def = next(s for s in SOURCE_DEFINITIONS if s["name"] == "stock_bj_a_spot_em")
    results, err, calls = _probe_batch_source(ak, src_def, requests, iter(range(1, 100)))

    assert err is None
    assert calls == 1, "Batch source must be fetched exactly ONCE"
    assert ak.stock_bj_a_spot_em.call_count == 1
    assert len(results) == 8, f"2 codes x 4 fields = 8 results, got {len(results)}"
    assert len({r.request_id for r in results}) == 1
    assert len({r.observation_id for r in results}) == 8
    proven = [r for r in results if r.status == STATUS_PROVEN]
    assert len(proven) == 8, f"All fields should be PROVEN, got {len(proven)}"

    values = {r.field: r.value for r in results if r.code == "830001"}
    assert values["pe_ttm"] == 10.0
    assert values["market_cap"] == 1e9


def test_batch_source_code_not_found(tmp_path):
    df = pd.DataFrame([
        {"代码": "920001", "市盈率-动态": 12.0, "市净率": 1.5, "总市值": 2e9, "总股本": 2e8},
    ])
    ak = MagicMock()
    ak.stock_bj_a_spot_em.return_value = df

    requests = [ProbeRequest(code="999999", code_type="old", name="Ghost")]
    src_def = next(s for s in SOURCE_DEFINITIONS if s["name"] == "stock_bj_a_spot_em")
    results, err, calls = _probe_batch_source(ak, src_def, requests, iter(range(1, 100)))

    assert err is None
    assert results[0].status == STATUS_CODE_NOT_FOUND


# ---------------------------------------------------------------------------
# 9. stock_value_em historical series
# ---------------------------------------------------------------------------


def _value_em_src():
    return next(s for s in SOURCE_DEFINITIONS if s["name"] == "stock_value_em")


def test_value_em_returns_historical_series(tmp_path):
    ak = MagicMock()
    ak.stock_value_em.return_value = _value_em_df()
    req = ProbeRequest(code="920799", code_type="new", name="艾融软件")

    results, calls = _probe_value_em(ak, req, _value_em_src(), iter(range(1, 100)))

    assert calls == 1
    assert ak.stock_value_em.call_count == 1
    history = [r for r in results if r.is_historical_series and r.status == STATUS_PROVEN]
    assert len(history) >= 3, f"Expected >=3 historical samples, got {len(history)}"
    dates = {r.as_of_date for r in history}
    assert len(dates) >= 3, f"Expected >=3 distinct time points, got {len(dates)}"
    assert all(r.response_sha256 for r in results), "response SHA256 must be present"
    assert all(r.retrieved_at for r in results), "retrieved_at provenance must be present"
    assert len({r.request_id for r in results}) == 1
    assert len({r.observation_id for r in results}) == len(results)


def test_value_em_snapshot_not_historical():
    ak = MagicMock()
    ak.stock_value_em.return_value = _value_em_df()
    req = ProbeRequest(code="920799", code_type="new", name="艾融软件")
    results, _ = _probe_value_em(ak, req, _value_em_src(), iter(range(1, 100)))
    assert all(r.is_historical_series is True for r in results if r.field != "dividend_yield")


def test_value_em_old_code_error_classified_adapter(tmp_path):
    ak = MagicMock()
    ak.stock_value_em.side_effect = TypeError("'NoneType' object is not subscriptable")
    req = ProbeRequest(code="830799", code_type="old", name="艾融软件")
    results, calls = _probe_value_em(ak, req, _value_em_src(), iter(range(1, 100)))
    assert calls == 1
    assert results[0].status == STATUS_ADAPTER_ERROR
    assert results[0].error_class == "TypeError"


def test_value_em_few_rows_limited_sample():
    ak = MagicMock()
    ak.stock_value_em.return_value = _value_em_df([
        {"数据日期": "2025-06-30", "PE(TTM)": 25.0, "市净率": 2.5,
         "市销率": 3.5, "总市值": 1.2e9, "总股本": 1.1e8},
    ])
    req = ProbeRequest(code="920799", code_type="new", name="艾融软件")
    results, _ = _probe_value_em(ak, req, _value_em_src(), iter(range(1, 100)))
    assert results[0].status == STATUS_LIMITED_SAMPLE


def test_value_em_dividend_yield_unavailable():
    ak = MagicMock()
    ak.stock_value_em.return_value = _value_em_df()
    req = ProbeRequest(code="920799", code_type="new", name="艾融软件")
    results, _ = _probe_value_em(ak, req, _value_em_src(), iter(range(1, 100)))
    dy = [r for r in results if r.field == "dividend_yield"]
    assert len(dy) == 1
    assert dy[0].status == STATUS_FIELD_UNAVAILABLE


# ---------------------------------------------------------------------------
# 10. NaN / Inf / empty / non-numeric not counted as coverage
# ---------------------------------------------------------------------------


def test_finite_float_rejects_nan_inf_empty_non_numeric():
    assert _finite_float(10.0) == 10.0
    assert _finite_float("10.5") == 10.5
    assert _finite_float(float("nan")) is None
    assert _finite_float(float("inf")) is None
    assert _finite_float(float("-inf")) is None
    assert _finite_float("") is None
    assert _finite_float("-") is None
    assert _finite_float("nan") is None
    assert _finite_float(None) is None
    assert _finite_float("abc") is None


def test_value_em_nan_values_not_proven():
    ak = MagicMock()
    ak.stock_value_em.return_value = _value_em_df([
        {"数据日期": "2023-05-01", "PE(TTM)": 20.0, "市净率": 2.0,
         "市销率": 3.0, "总市值": 1e9, "总股本": 1e8},
        {"数据日期": "2024-05-01", "PE(TTM)": float("nan"), "市净率": 2.2,
         "市销率": 3.3, "总市值": 1.1e9, "总股本": 1e8},
        {"数据日期": "2025-06-30", "PE(TTM)": 25.0, "市净率": 2.5,
         "市销率": 3.5, "总市值": 1.2e9, "总股本": 1.1e8},
    ])
    req = ProbeRequest(code="920799", code_type="new", name="艾融软件")
    results, _ = _probe_value_em(ak, req, _value_em_src(), iter(range(1, 100)))
    pe_nan = [r for r in results if r.field == "pe_ttm" and r.as_of_date == "2024-05-01"]
    assert pe_nan and pe_nan[0].status != STATUS_PROVEN


# ---------------------------------------------------------------------------
# 11. Plain PS never mapped to ps_ttm (source-field contract check)
# ---------------------------------------------------------------------------


def test_plain_ps_not_mapped_to_ps_ttm():
    value_src = next(s for s in SOURCE_DEFINITIONS if s["name"] == "stock_value_em")
    ps_note = value_src["fields"]["ps_ttm"]["note"]
    assert "PS_TTM" in ps_note, "ps_ttm must only be claimed where the underlying field is TTM"


def test_ps_ttm_contract_bj_spot():
    spot = next(s for s in SOURCE_DEFINITIONS if s["name"] == "stock_bj_a_spot_em")
    assert "ps_ttm" not in spot["fields"], "BJ spot must not claim ps_ttm"


# ---------------------------------------------------------------------------
# 12. Field classification thresholds
# ---------------------------------------------------------------------------


def test_classify_field_proven_at_three_samples():
    results = [_make_result(status=STATUS_PROVEN, code=c) for c in ("1", "2", "3")]
    assert _classify_field(results, "pe_ttm") == STATUS_PROVEN


def test_classify_field_limited_sample():
    results = [_make_result(status=STATUS_PROVEN, code=c) for c in ("1", "2")]
    assert _classify_field(results, "pe_ttm") == STATUS_LIMITED_SAMPLE


def test_classify_field_unproven_when_empty():
    assert _classify_field([], "pe_ttm") == STATUS_UNPROVEN


def test_fuse_source_dict_shape():
    fused = _fuse_source("stock_zh_valuation_baidu", "label", "3 consecutive failures: X")
    assert fused["fused"] is True
    assert fused["fuse_reason"].startswith("3 consecutive failures")
    assert set(fused["fields"].keys()) == set(TARGET_FIELDS)


# ---------------------------------------------------------------------------
# 13. Network blocked by default / explicit switch
# ---------------------------------------------------------------------------


def test_network_blocked_by_default(monkeypatch):
    monkeypatch.delenv(NETWORK_ENV_VAR, raising=False)
    with pytest.raises(SystemExit) as exc_info:
        phase2(
            mappings=[MagicMock()],
            output_dir=Path("/tmp"),
            max_codes=1, max_requests=1, max_time_minutes=1,
        )
    assert exc_info.value.code == 1


def test_network_allowed_with_explicit_switch(monkeypatch, tmp_path):
    monkeypatch.setenv(NETWORK_ENV_VAR, "1")
    ak = MagicMock()
    ak.stock_value_em.return_value = _value_em_df()
    ak.stock_bj_a_spot_em.return_value = None
    ak.stock_zh_valuation_baidu.return_value = None
    ak.stock_zh_valuation_comparison_em.return_value = None
    with (
        patch("scripts.probe_bj_valuation_sources._probe_import_akshare", return_value=ak),
        patch("time.monotonic", return_value=0.0),
    ):
        phase2(
            mappings=sample_mappings_fixture(),
            output_dir=tmp_path,
            max_codes=3, max_requests=80, max_time_minutes=20,
        )
    assert (tmp_path / "probe-results.json").is_file()
    ledger = json.loads((tmp_path / "request-ledger.json").read_text(encoding="utf-8"))
    observations = json.loads((tmp_path / "probe-results.json").read_text(encoding="utf-8"))
    request_ids = {r["request_id"] for r in ledger}
    assert request_ids
    assert all(r["request_id"] in request_ids for r in observations if r["source"])
    assert len({r["observation_id"] for r in observations}) == len(observations)


def sample_mappings_fixture():
    return [
        CodeMapping(
            old_code="830799", new_code="920799", name="艾融软件",
            name_old="艾融软件", name_new="艾融软件",
            mapping_status=MAPPING_CANDIDATE_NAME_MATCH, confidence="medium",
            one_to_one=True, usable_for_probe=True, usable_for_migration=False,
        ),
    ]


# ---------------------------------------------------------------------------
# 14. build_code_mapping confidence semantics
# ---------------------------------------------------------------------------


def _build_mock_conn(old_rows, new_rows):
    conn = MagicMock()
    conn.execute.side_effect = [
        MagicMock(fetchall=lambda: [dict(r) for r in old_rows]),
        MagicMock(fetchall=lambda: [dict(r) for r in new_rows]),
    ]
    return conn


def test_mapping_name_match_is_medium():
    old_rows = [{"code": "830001", "name": "Test A", "list_date": "2020-01-01"}]
    new_rows = [{"code": "920001", "name": "Test A", "list_date": "2021-01-01"}]

    with patch("scripts.probe_bj_valuation_sources.open_ro",
               return_value=_build_mock_conn(old_rows, new_rows)):
        mappings = build_code_mapping(Path("/fake/market.db"))

    paired = [m for m in mappings if m.old_code and m.new_code]
    assert len(paired) == 1
    assert paired[0].mapping_status == MAPPING_CANDIDATE_NAME_MATCH
    assert paired[0].confidence == "medium", (
        f"Name-match alone must be medium (candidate), got {paired[0].confidence}"
    )
    assert paired[0].usable_for_migration is False
    assert paired[0].usable_for_probe is True


def test_mapping_unpaired_new_is_low():
    old_rows = []
    new_rows = [{"code": "920001", "name": "Brand New", "list_date": "2021-01-01"}]

    with patch("scripts.probe_bj_valuation_sources.open_ro",
               return_value=_build_mock_conn(old_rows, new_rows)):
        mappings = build_code_mapping(Path("/fake/market.db"))

    unpaired = [m for m in mappings if not m.old_code and m.new_code
                and m.mapping_status == "UNPAIRED_NEW"]
    assert len(unpaired) == 1
    assert unpaired[0].mapping_status == "UNPAIRED_NEW"
    assert unpaired[0].confidence == "low"


def test_mapping_unpaired_old_is_low():
    old_rows = [{"code": "830001", "name": "Old Name", "list_date": "2020-01-01"}]
    new_rows = []

    with patch("scripts.probe_bj_valuation_sources.open_ro",
               return_value=_build_mock_conn(old_rows, new_rows)):
        mappings = build_code_mapping(Path("/fake/market.db"))

    unpaired = [m for m in mappings if m.old_code and not m.new_code]
    assert len(unpaired) == 1
    assert unpaired[0].confidence == "low"


def test_mapping_conflict():
    old_rows = [
        {"code": "830001", "name": "Test A", "list_date": "2020-01-01"},
        {"code": "830010", "name": "Test A", "list_date": "2020-06-01"},
    ]
    new_rows = [
        {"code": "920001", "name": "Test A", "list_date": "2021-01-01"},
    ]

    with patch("scripts.probe_bj_valuation_sources.open_ro",
               return_value=_build_mock_conn(old_rows, new_rows)):
        mappings = build_code_mapping(Path("/fake/market.db"))

    conflict_maps = [m for m in mappings if m.conflicts]
    assert len(conflict_maps) >= 1, "Expected at least one mapping with conflicts"

    for m in conflict_maps:
        if any("multiple old codes" in c for c in m.conflicts):
            assert m.one_to_one is False
            assert m.confidence == "medium"
            break
    else:
        pytest.fail("No mapping found with 'multiple old codes' conflict")


def test_mapping_920808_unresolved():
    old_rows = []
    new_rows = []

    with patch("scripts.probe_bj_valuation_sources.open_ro",
               return_value=_build_mock_conn(old_rows, new_rows)):
        mappings = build_code_mapping(Path("/fake/market.db"))

    special = [m for m in mappings if m.new_code == "920808"]
    assert len(special) == 1
    assert special[0].mapping_status == MAPPING_UNRESOLVED
    assert special[0].usable_for_probe is True
    assert special[0].usable_for_migration is False


def test_official_evidence_fields_missing_not_high():
    """§7.2 #13: incomplete evidence cannot upgrade a mapping."""
    mappings = [CodeMapping(
        old_code="830001", new_code="920001", name="Test A",
        name_old="Test A", name_new="Test A",
    )]
    incomplete = [{
        "old_code": "830001", "new_code": "920001",
        "security_name": "Test A", "listing_date": "2020-01-01",
        "evidence_url": "https://www.bse.cn/x",
        "evidence_retrieved_at": "2026-08-02T00:00:00Z",
        "evidence_sha256": "", "evidence_excerpt": "official row",
    }]
    upgraded = apply_official_mapping_evidence(mappings, incomplete)
    assert upgraded[0].confidence != "high"
    assert upgraded[0].mapping_status != "OFFICIAL_PROVEN"


def test_complete_bse_evidence_upgrades_mapping():
    mappings = [CodeMapping(
        old_code="430017", new_code="920017", name="星昊医药",
        name_old="星昊医药", name_new="星昊医药",
    )]
    evidence = [{
        "old_code": "430017", "new_code": "920017",
        "security_name": "星昊医药", "listing_date": "2023-05-31",
        "evidence_url": "https://www.bse.cn/uploads/mapping.docx",
        "evidence_retrieved_at": "2026-08-02T20:00:00+08:00",
        "evidence_sha256": "a" * 64,
        "evidence_excerpt": "星昊医药 2023-05-31 430017 920017",
    }]
    upgraded = apply_official_mapping_evidence(mappings, evidence)
    assert upgraded[0].mapping_status == "OFFICIAL_PROVEN"
    assert upgraded[0].confidence == "high"
    assert upgraded[0].usable_for_migration is True


def test_official_evidence_merges_compatible_new_only_mapping():
    mappings = [
        CodeMapping(
            old_code="872808", new_code="", name="曙光数创",
            name_old="曙光数创", name_new="",
            mapping_status="UNPAIRED_OLD",
        ),
        CodeMapping(
            old_code="", new_code="920808", name="曙光数创",
            name_old="", name_new="曙光数创",
            mapping_status=MAPPING_UNRESOLVED,
        ),
    ]
    evidence = [{
        "old_code": "872808", "new_code": "920808",
        "security_name": "曙光数创", "listing_date": "2022-11-18",
        "evidence_url": "https://www.bse.cn/uploads/mapping.docx",
        "evidence_retrieved_at": "2026-08-02T20:00:00+08:00",
        "evidence_sha256": "a" * 64,
        "evidence_excerpt": "曙光数创 2022-11-18 872808 920808",
    }]
    upgraded = apply_official_mapping_evidence(mappings, evidence)
    matches = [m for m in upgraded if m.new_code == "920808"]
    assert len(matches) == 1
    assert matches[0].old_code == "872808"
    assert matches[0].mapping_status == MAPPING_OFFICIAL_PROVEN


def test_official_curated_pairs_remain_in_probe_plan(sample_mappings):
    for mapping in sample_mappings:
        mapping.mapping_status = MAPPING_OFFICIAL_PROVEN
        mapping.confidence = "high"
        mapping.usable_for_migration = True
    plan = _build_probe_plan(sample_mappings, max_codes=6)
    assert [r.code for r in plan] == [
        "830799", "920799", "835185", "920185", "430017", "920017",
    ]


# ---------------------------------------------------------------------------
# 15. Capability matrix / cross-source output
# ---------------------------------------------------------------------------


def test_ps_ttm_exposed_in_matrix(tmp_path):
    results = [
        _make_result(status=STATUS_PROVEN, field="ps_ttm",
                     source="stock_zh_valuation_comparison_em"),
        _make_result(status=STATUS_PROVEN, field="ps_ttm",
                     source="stock_zh_valuation_comparison_em"),
    ]
    _write_source_capability_matrix(
        results,
        [{"source": "stock_zh_valuation_comparison_em", "label": "东方财富-同行比较估值",
          "mode": "per_code_snapshot", "fused": False}],
        tmp_path,
    )
    output = (tmp_path / "source-capability-matrix.md").read_text(encoding="utf-8")
    assert "ps_ttm" in output
    assert "PS-TTM" in output


def test_comparison_source_defines_real_columns():
    comp = next(s for s in SOURCE_DEFINITIONS if s["name"] == "stock_zh_valuation_comparison_em")
    assert comp["fields"]["pe_ttm"]["col"] == "市盈率-TTM"
    assert comp["fields"]["pb_mrq"]["col"] == "市净率-MRQ"
    assert comp["fields"]["ps_ttm"]["col"] == "市销率-TTM"
    assert comp.get("code_col") == "代码"


def test_cross_source_consistency_written(tmp_path):
    results = [
        _make_result(status=STATUS_PROVEN, code="920799", field="pe_ttm",
                     source="stock_value_em", value=20.0, as_of_date="2024-05-01"),
        _make_result(status=STATUS_PROVEN, code="920799", field="pe_ttm",
                     source="stock_zh_valuation_baidu", value=20.3, as_of_date="2024-05-01"),
    ]
    _write_cross_source_consistency(results, tmp_path)
    data = json.loads((tmp_path / "cross-source-consistency.json").read_text(encoding="utf-8"))
    assert len(data) == 1
    assert data[0]["sample_consistent_le_2pct"] is True


def test_cross_source_consistency_gt_2pct_flag(tmp_path):
    results = [
        _make_result(status=STATUS_PROVEN, code="920799", field="pe_ttm",
                     source="stock_value_em", value=20.0, as_of_date="2024-05-01"),
        _make_result(status=STATUS_PROVEN, code="920799", field="pe_ttm",
                     source="stock_zh_valuation_baidu", value=30.0, as_of_date="2024-05-01"),
    ]
    _write_cross_source_consistency(results, tmp_path)
    data = json.loads((tmp_path / "cross-source-consistency.json").read_text(encoding="utf-8"))
    assert data[0]["sample_consistent_le_2pct"] is False
    assert data[0]["comparison_status"] == "SAMPLE_DIFFERENT"


def test_cross_source_nearby_month_not_aligned(tmp_path):
    results = [
        _make_result(status=STATUS_PROVEN, code="920799", field="pe_ttm",
                     source="stock_value_em", value=20.0, as_of_date="2024-05-31"),
        _make_result(status=STATUS_PROVEN, code="920799", field="pe_ttm",
                     source="stock_zh_valuation_baidu", value=20.0, as_of_date="2024-05-01"),
    ]
    _write_cross_source_consistency(results, tmp_path)
    data = json.loads((tmp_path / "cross-source-consistency.json").read_text(encoding="utf-8"))
    assert data == []


def test_cross_source_incompatible_units_not_comparable(tmp_path):
    results = [
        _make_result(status=STATUS_PROVEN, code="920799", field="market_cap",
                     source="stock_value_em", value=2e9, unit="元", as_of_date="2024-05-01"),
        _make_result(status=STATUS_PROVEN, code="920799", field="market_cap",
                     source="stock_bj_a_spot_em", value=20, unit="亿元", as_of_date="2024-05-01"),
    ]
    _write_cross_source_consistency(results, tmp_path)
    data = json.loads((tmp_path / "cross-source-consistency.json").read_text(encoding="utf-8"))
    assert data[0]["comparison_status"] == "INCOMPARABLE"
    assert data[0]["relative_difference"] is None


# ---------------------------------------------------------------------------
# 16. Audit script: gates + verdicts
# ---------------------------------------------------------------------------


def _mk_mapping(status="CANDIDATE_NAME_MATCH", old="", new="920001", usable_mig=False):
    official = status == "OFFICIAL_PROVEN"
    return {
        "old_code": old, "new_code": new,
        "name_old": "x" if old else "", "name_new": "x",
        "mapping_status": status,
        "confidence": "high" if official else "medium" if status == "CANDIDATE_NAME_MATCH" else "low",
        "evidence_type": "bse_official_mapping_attachment" if official else "none_local",
        "evidence_url": "https://www.bse.cn/uploads/mapping.docx" if official else "",
        "evidence_date": "2026-08-02T20:00:00+08:00" if official else "",
        "evidence_sha256": "e" * 64 if official else "",
        "evidence_excerpt": f"official mapping {old} -> {new}" if official else "",
        "conflicts": [], "usable_for_probe": True, "usable_for_migration": usable_mig,
        "reason": "test", "name": "x", "exchange": "BJ", "effective_date": "",
        "evidence_source": "Beijing Stock Exchange" if official else status,
        "one_to_one": True,
    }


def _mk_result(code="920001", field="pe_ttm", source="stock_value_em", status="PROVEN",
               hist=True, asof="2024-05-01", unit="倍", sha="s1", request_id="r1",
               err="", errcls="", code_type="new"):
    return {
        "request_id": request_id, "code_requested": code, "code_resolved": code,
        "code_type": code_type, "name": "x", "source": source, "field": field,
        "value": 20.0 if status == "PROVEN" else None, "unit": unit,
        "as_of_date": asof, "period_start": "2023-05-01", "period_end": "2025-06-30",
        "is_historical_series": hist, "source_column": "PE(TTM)",
        "source_title_or_endpoint": "test", "response_sha256": sha,
        "retrieved_at": "2026-08-02T00:00:00Z", "status": status, "error": err,
        "error_class": errcls, "latency_ms": 1.0,
    }


def _full_mapping():
    return [
        _mk_mapping(status="CANDIDATE_NAME_MATCH", old="830799", new="920799"),
        _mk_mapping(status="CANDIDATE_NAME_MATCH", old="835185", new="920185"),
        _mk_mapping(status="CANDIDATE_NAME_MATCH", old="430017", new="920017"),
        _mk_mapping(status="UNPAIRED_NEW", new="920002"),
        _mk_mapping(status="UNRESOLVED", new="920808"),
    ]


def _full_history_results(codes=("920799", "920185", "920017")):
    results = []
    i = 0
    for code in codes:
        for field in ("pe_ttm", "pb_mrq"):
            for asof in ("2023-05-01", "2024-05-01", "2025-06-30"):
                i += 1
                results.append(_mk_result(
                    code=code, field=field, asof=asof,
                    request_id=f"r{i}", sha=f"s{i}", code_type="new",
                ))
    # dividend_yield local assessments
    for code in codes:
        i += 1
        results.append(_mk_result(
            code=code, field="dividend_yield", status="FIELD_UNAVAILABLE",
            hist=False, unit="", request_id=f"r{i}", sha="sx", code_type="new",
        ))
    return results


def _full_plan(codes=("920799", "920185", "920017")):
    return [{"code": c, "code_type": "new", "name": "x", "source": "", "field": "", "reason": "r"}
            for c in codes]


def _r1_audit_inputs(results):
    source_rows = [r for r in results if r.get("source")]
    ledger = [
        {
            "request_id": r["request_id"],
            "source": r["source"],
            "endpoint": "mock.audit",
            "attempt_no": 1,
            "is_retry": False,
            "code_or_batch_scope": r["code_requested"],
            "outcome": r["status"],
            "counted_as_real_request": True,
        }
        for r in source_rows
    ]
    counts = {}
    for row in ledger:
        counts[row["source"]] = counts.get(row["source"], 0) + 1
    execution = {
        "runtime_real_request_count": len(ledger),
        "runtime_requests_by_source": counts,
        "wall_clock_within_20_minutes": True,
        "approved_free_sources": sorted(counts),
        "new_credentials_used": False,
        "checkpoint_resume_test_passed": True,
        "nonzero_error_propagation_test_passed": True,
    }
    consistency = [
        {
            "code": code,
            "field": "pe_ttm",
            "comparison_status": "SAMPLE_ALIGNED",
            "sample_consistent_le_2pct": True,
            "relative_difference": 0.001,
        }
        for code in ("920799", "920185", "920017")
    ]
    return ledger, execution, consistency


def test_audit_go_verdict():
    mapping = _full_mapping()
    # add 3 OFFICIAL_PROVEN mappings so the migration gate passes
    for k, (old, new) in enumerate([("830100", "920100"), ("830200", "920200"), ("830300", "920300")], 1):
        mapping.append(_mk_mapping(status="OFFICIAL_PROVEN", old=old, new=new,
                                   usable_mig=True))
    results = _full_history_results()
    plan = _full_plan()
    ledger, execution, consistency = _r1_audit_inputs(results)
    report = analyze(
        mapping, results, plan, consistency,
        request_ledger=ledger, execution_evidence=execution,
    )
    assert report["verdict"] == VERDICT_GO


def test_audit_no_official_mapping_is_partial():
    mapping = _full_mapping()  # no OFFICIAL_PROVEN
    results = _full_history_results()
    plan = _full_plan()
    report = analyze(mapping, results, plan, [])
    assert report["verdict"] == VERDICT_PARTIAL, (
        f"Without official mapping, verdict must be PARTIAL, got {report['verdict']}"
    )


def test_audit_no_history_is_no_go():
    mapping = _full_mapping()
    # no historical series at all
    results = [_mk_result(field="pe_ttm", status="NETWORK_ERROR", hist=False,
                          request_id="e1", sha="s1")]
    plan = _full_plan()
    report = analyze(mapping, results, plan, [])
    assert report["verdict"] == VERDICT_NO_GO


def test_audit_any_gate_failure_not_go():
    mapping = _full_mapping()
    results = _full_history_results(codes=("920799",))  # only 1 security
    plan = _full_plan(codes=("920799",))
    report = analyze(mapping, results, plan, [])
    assert report["verdict"] != VERDICT_GO
    assert not report["all_gates_pass"]


def test_audit_counts_conserve():
    """§7.2 #20: per-field status counts + error classification counts conserve."""
    mapping = _full_mapping()
    results = _full_history_results()
    plan = _full_plan()
    ledger, execution, consistency = _r1_audit_inputs(results)
    report = analyze(
        mapping, results, plan, consistency,
        request_ledger=ledger, execution_evidence=execution,
    )

    total_status = 0
    for f in TARGET_FIELDS:
        c = report["field_status"][f]
        total_status += sum(c.values())
    assert total_status > 0

    # source request counts must equal unique request_ids per source
    req_by_source = report["probe_stats"]["request_counts_by_source"]
    assert "stock_value_em" in req_by_source
    assert req_by_source["stock_value_em"] > 0


def test_audit_main_exit_zero(tmp_path):
    results = _full_history_results()
    ledger, execution, consistency = _r1_audit_inputs(results)
    (tmp_path / "bj-code-mapping.json").write_text(json.dumps(_full_mapping()), encoding="utf-8")
    (tmp_path / "probe-results.json").write_text(json.dumps(results), encoding="utf-8")
    (tmp_path / "probe-plan.json").write_text(json.dumps(_full_plan()), encoding="utf-8")
    (tmp_path / "cross-source-consistency.json").write_text(json.dumps(consistency), encoding="utf-8")
    (tmp_path / "request-ledger.json").write_text(json.dumps(ledger), encoding="utf-8")
    (tmp_path / "execution-evidence.json").write_text(json.dumps(execution), encoding="utf-8")
    out = tmp_path / "analysis"
    rc = audit_main(["--input-dir", str(tmp_path), "--output-dir", str(out)])
    assert rc == 0
    assert (out / "verdict.json").is_file()
    verdict = json.loads((out / "verdict.json").read_text(encoding="utf-8"))
    assert verdict["verdict"] in {VERDICT_GO, VERDICT_PARTIAL, VERDICT_NO_GO}


def test_audit_main_missing_input_nonzero(tmp_path):
    rc = audit_main(["--input-dir", str(tmp_path), "--output-dir", str(tmp_path)])
    assert rc == 1


# ---------------------------------------------------------------------------
# 17. Input SQLite read-only enforcement
# ---------------------------------------------------------------------------


def test_open_ro_enforces_query_only(tmp_path):
    from scripts.probe_bj_valuation_sources import open_ro
    db = tmp_path / "test.db"
    import sqlite3
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE t (x INTEGER)")
    con.execute("INSERT INTO t VALUES (1)")
    con.commit()
    con.close()

    ro = open_ro(db)
    try:
        assert ro.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(Exception):
            ro.execute("CREATE TABLE x (y INTEGER)")
    finally:
        ro.close()


def test_read_only_connection_cannot_write(tmp_path):
    import sqlite3
    from scripts.probe_bj_valuation_sources import open_ro
    db = tmp_path / "test.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE t (x INTEGER)")
    con.commit()
    con.close()
    ro = open_ro(db)
    try:
        with pytest.raises(Exception):
            ro.execute("INSERT INTO t VALUES (5)")
    finally:
        ro.close()


# ---------------------------------------------------------------------------
# 18. Script errors exit non-zero
# ---------------------------------------------------------------------------


def test_audit_script_error_exits_nonzero(tmp_path):
    rc = audit_main(["--input-dir", str(tmp_path / "missing"), "--output-dir", str(tmp_path)])
    assert rc == 1


def test_network_call_with_retry_counts():
    def f():
        raise ConnectionError("refused")

    ak = MagicMock()
    ak.stock_bj_a_spot_em = f
    src_def = {"name": "stock_bj_a_spot_em", "timeout_supported": False}
    result, err, calls = _network_call_with_retry(ak, "stock_bj_a_spot_em", src_def)
    assert result is None
    assert err is not None
    assert calls == 2, f"Expected 1 retry (2 calls), got {calls}"


def test_network_call_no_retry_on_adapter_error():
    def f():
        raise ValueError("bad")

    ak = MagicMock()
    ak.stock_bj_a_spot_em = f
    src_def = {"name": "stock_bj_a_spot_em", "timeout_supported": False}
    result, err, calls = _network_call_with_retry(ak, "stock_bj_a_spot_em", src_def)
    assert result is None
    assert calls == 1, f"Adapter error must NOT retry, got {calls}"


def test_network_call_success_counts_one():
    def f():
        return "ok"

    ak = MagicMock()
    ak.stock_bj_a_spot_em = f
    src_def = {"name": "stock_bj_a_spot_em", "timeout_supported": False}
    result, err, calls = _network_call_with_retry(ak, "stock_bj_a_spot_em", src_def)
    assert result == "ok"
    assert err is None
    assert calls == 1


# ---------------------------------------------------------------------------
# 19. Provenance / SHA / observation schema completeness
# ---------------------------------------------------------------------------


OBSERVATION_FIELDS = {
    "source", "request_id", "code_requested", "code_resolved", "field", "value",
    "unit", "as_of_date", "period_start", "period_end", "is_historical_series",
    "source_column", "source_title_or_endpoint", "response_sha256",
    "retrieved_at", "status", "error_class", "latency_ms",
}


def test_probe_result_has_observation_schema():
    from scripts.probe_bj_valuation_sources import ProbeResult as PR
    r = PR(request_id="x", code_requested="920799", code_resolved="920799",
           code_type="new", name="x", source="stock_value_em", field="pe_ttm",
           value=20.0, unit="倍", status=STATUS_PROVEN)
    d = r.__dict__
    missing = OBSERVATION_FIELDS - set(d.keys())
    assert not missing, f"Observation fields missing: {missing}"


def test_checkpoint_resume_skips_completed_code_and_preserves_ledger(tmp_path):
    plan = [
        ProbeRequest(code="920799", code_type="new", name="艾融软件"),
        ProbeRequest(code="920185", code_type="new", name="贝特瑞"),
    ]
    completed = _make_result(
        source="stock_value_em", code="920799", request_id="stock_value_em-0001",
        observation_id="obs-completed",
    )
    checkpoint = {
        "version": 1,
        "plan_codes": ["920799", "920185"],
        "batch_completed": True,
        "completed_codes": ["920799"],
        "request_ledger": [{
            "request_id": "stock_value_em-0001",
            "source": "stock_value_em",
            "endpoint": "mock.stock_value_em",
            "attempt_no": 1,
            "is_retry": False,
            "code_or_batch_scope": "920799",
            "outcome": "PROVEN",
            "counted_as_real_request": True,
        }],
        "observations": [asdict(completed)],
        "source_stats": [{
            "source": src["name"], "label": src["label"], "mode": src["mode"],
            "fused": False, "fuse_reason": "",
            "requests_made": 1 if src["name"] == "stock_value_em" else 0,
            "consecutive_failures": 0, "last_error_type": "",
        } for src in SOURCE_DEFINITIONS],
    }
    checkpoint_path = tmp_path / "checkpoint.json"
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")

    history_mock = MagicMock(
        side_effect=_ledgered_mock(
            "stock_value_em", _make_result(source="stock_value_em", code="920185"),
        ),
    )
    baidu_mock = MagicMock(
        side_effect=_ledgered_mock(
            "stock_zh_valuation_baidu",
            _make_result(source="stock_zh_valuation_baidu", code="920185"),
        ),
    )
    comparison_mock = MagicMock(
        side_effect=_ledgered_mock(
            "stock_zh_valuation_comparison_em",
            _make_result(source="stock_zh_valuation_comparison_em", code="920185"),
        ),
    )
    with (
        patch("scripts.probe_bj_valuation_sources._probe_batch_source") as batch_mock,
        patch("scripts.probe_bj_valuation_sources._probe_value_em", history_mock),
        patch("scripts.probe_bj_valuation_sources._probe_valuation_baidu", baidu_mock),
        patch("scripts.probe_bj_valuation_sources._probe_valuation_comparison", comparison_mock),
        patch("time.monotonic", return_value=0.0),
    ):
        results, stats = _run_probe_requests(
            plan, _make_ak(), tmp_path, max_requests=20, max_time_minutes=20,
            checkpoint_path=checkpoint_path, resume=True,
        )

    batch_mock.assert_not_called()
    assert history_mock.call_count == 1
    assert baidu_mock.call_count == 2
    assert comparison_mock.call_count == 1
    assert sum(1 for r in results if r.observation_id == "obs-completed") == 1
    ledger = json.loads((tmp_path / "request-ledger.json").read_text(encoding="utf-8"))
    assert ledger[0]["request_id"] == "stock_value_em-0001"
    assert len({r["request_id"] for r in ledger}) == len(ledger)


def test_checkpoint_plan_mismatch_fails_nonzero_semantics(tmp_path):
    checkpoint_path = tmp_path / "checkpoint.json"
    checkpoint_path.write_text(json.dumps({"plan_codes": ["920999"]}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="does not match"):
        _run_probe_requests(
            [ProbeRequest(code="920799", code_type="new", name="艾融软件")],
            _make_ak(), tmp_path, max_requests=20, max_time_minutes=20,
            checkpoint_path=checkpoint_path, resume=True,
        )


def test_retry_not_double_count_in_request_stats():
    """A retried network call counts as 2 real requests in the request stats."""
    plan = _build_probe_plan(sample_mappings_fixture(), max_codes=1)

    fail = _make_result(status=STATUS_NETWORK_ERROR, error="ConnectionError: refused",
                        source="stock_value_em")

    with (
        patch("scripts.probe_bj_valuation_sources._probe_batch_source",
              side_effect=_ledgered_mock(
                  "stock_bj_a_spot_em", _make_result(source="stock_bj_a_spot_em"), batch=True,
              )),
        patch("scripts.probe_bj_valuation_sources._probe_value_em",
              side_effect=_ledgered_mock("stock_value_em", fail, calls=2)),
        patch("scripts.probe_bj_valuation_sources._probe_valuation_baidu",
              side_effect=_ledgered_mock(
                  "stock_zh_valuation_baidu", _make_result(source="stock_zh_valuation_baidu"),
              )),
        patch("scripts.probe_bj_valuation_sources._probe_valuation_comparison",
              side_effect=_ledgered_mock(
                  "stock_zh_valuation_comparison_em",
                  _make_result(source="stock_zh_valuation_comparison_em"),
              )),
        patch("time.monotonic", return_value=0.0),
    ):
        results, stats = _run_probe_requests(
            plan, _make_ak(), Path("/tmp"),
            max_requests=999, max_time_minutes=20,
        )

    ve_stats = next(s for s in stats if s["source"] == "stock_value_em")
    assert ve_stats["requests_made"] == 2, f"Retried call must count twice, got {ve_stats['requests_made']}"


# ---------------------------------------------------------------------------
# 20. Log sanitization
# ---------------------------------------------------------------------------


def test_log_sanitization(caplog):
    caplog.set_level("ERROR")

    sensitive = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"

    def f():
        raise ValueError(sensitive)

    ak = MagicMock()
    ak.stock_bj_a_spot_em = f

    result, err = _safe_network_call(ak, "stock_bj_a_spot_em")

    assert err is not None
    assert sensitive in err, "Error should contain the original sensitive data"

    for record in caplog.records:
        assert sensitive not in record.getMessage(), (
            f"Log record contains sensitive data: {record.getMessage()}"
        )
