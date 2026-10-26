"""Tests for B1-R1 audit_security_identity_evidence.py."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent / "scripts"))

# Import after path setup
from audit_security_identity_evidence import (
    CrawlBudget,
    _has_required_fields,
    build_evidence,
    classify_with_evidence,
    generate_sql,
    investigate_001235,
    open_ro,
    query_tencent_batch,
    validate_authoritative_evidence,
)


@pytest.fixture
def memory_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE stock_info (code TEXT, name TEXT, market TEXT, list_date TEXT, is_active INTEGER)")
    conn.execute("CREATE TABLE daily_klines (code TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, amount REAL)")
    conn.execute("INSERT INTO stock_info VALUES ('000001', '平安银行', 'SZ', '19910101', 1)")
    conn.execute("INSERT INTO stock_info VALUES ('000013', '*ST石化A', 'SZ', '19910101', 1)")
    conn.execute("INSERT INTO stock_info VALUES ('000015', 'PT中浩A', 'SZ', '19910101', 1)")
    conn.execute("INSERT INTO stock_info VALUES ('000024', '招商地产', 'SZ', '19910101', 1)")
    conn.execute("INSERT INTO stock_info VALUES ('001235', '无效里得', 'SZ', '20260518', 1)")
    conn.execute("INSERT INTO stock_info VALUES ('920808', '曙光数创', 'SZ', '20230101', 1)")
    conn.execute("INSERT INTO stock_info VALUES ('XXX', 'TEST', 'SZ', '20260101', 1)")
    for i in range(42):
        conn.execute("INSERT INTO daily_klines VALUES ('001235', '2026-05-18', 25.0, 26.0, 24.0, 25.5, 1000, 25000.0)")
    for i in range(13):
        conn.execute("INSERT INTO daily_klines VALUES ('000013', '2026-06-29', 2.0, 3.0, 1.0, 2.5, 500, 1000.0)")
    for d in ["2026-05-18", "2026-06-01", "2026-06-15"]:
        conn.execute("INSERT INTO daily_klines VALUES ('000024', ?, 10.0, 11.0, 9.0, 10.5, 1000, 10000.0)", (d,))
    conn.execute("INSERT INTO daily_klines VALUES ('920808', '2026-07-01', 55.0, 57.0, 54.0, 56.0, 5000, 280000.0)")
    conn.commit()
    return conn


def _tencent(**overrides) -> dict:
    base = {
        "name": "",
        "market": "SZ",
        "status": "normal",
        "price": "0.0",
        "source_url": "https://qt.gtimg.cn/q=sz000013",
        "response_sha256": "a" * 64,
        "retrieved_at": "2026-08-01T00:00:00+00:00",
    }
    base.update(overrides)
    return base


def test_open_ro_rejects_write():
    """Verify open_ro rejects write attempts."""
    path = Path("/tmp/test_b1_r1_ro.db")
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.close()
    conn2 = open_ro(path)
    with pytest.raises(sqlite3.OperationalError, match="attempt to write a readonly database"):
        conn2.execute("CREATE TABLE t2 (x INTEGER)")
    conn2.close()
    path.unlink(missing_ok=True)


def test_classify_920808_tencent_only_medium(memory_db):
    """920808 market fix is candidate but Tencent alone => at most medium, no SQL."""
    info = {"code": "920808", "name": "曙光数创", "market": "SZ", "list_date": "20230101", "is_active": 1}
    kline = {"kline_count": 1, "kline_min_date": "2026-07-01", "kline_max_date": "2026-07-01"}
    tencent = _tencent(name="曙光数创", market="BJ", status="normal")
    cat, conf, _ = classify_with_evidence("920808", info, kline, tencent, None, None)
    assert cat == "CORRECT_MARKET_LABEL"
    assert conf == "medium"


def test_classify_001235_manual_review(memory_db):
    """001235 should be UNKNOWN_MANUAL_REVIEW with klines + unlisted status."""
    info = {"code": "001235", "name": "无效里得", "market": "SZ", "list_date": "20260518", "is_active": 1}
    kline = {"kline_count": 42, "kline_min_date": "2026-05-18", "kline_max_date": "2026-07-17"}
    tencent = _tencent(name="无效里得", market="SZ", status="unlisted")
    cat, conf, _ = classify_with_evidence("001235", info, kline, tencent, "TEST_OR_POLLUTION", "medium")
    assert cat == "UNKNOWN_MANUAL_REVIEW"
    assert conf == "medium"


def test_classify_000013_delisted_single_secondary_medium(memory_db):
    """000013 delisted with single Tencent source => medium (not high)."""
    info = {"code": "000013", "name": "*ST石化A", "market": "SZ", "list_date": "19910101", "is_active": 1}
    kline = {"kline_count": 13, "kline_min_date": "2026-06-29", "kline_max_date": "2026-07-17"}
    tencent = _tencent(name="*ST石化A", market="SZ", status="delisted")
    cat, conf, _ = classify_with_evidence("000013", info, kline, tencent, "DELISTED_OR_LEGACY", "high")
    assert cat == "KEEP_HISTORICAL_INACTIVE"
    assert conf == "medium"


def test_classify_000024_successor_without_authoritative(memory_db):
    """000024 successor without authoritative evidence is downgraded, no high."""
    info = {"code": "000024", "name": "招商地产", "market": "SZ", "list_date": "19910101", "is_active": 1}
    kline = {"kline_count": 3, "kline_min_date": "2026-05-18", "kline_max_date": "2026-06-15"}
    tencent = _tencent(name="招商地产", market="SZ", status="normal")
    cat, conf, _ = classify_with_evidence("000024", info, kline, tencent, "KEEP_HISTORICAL_INACTIVE", "high")
    assert cat == "UNKNOWN_MANUAL_REVIEW"
    assert conf == "low"


def test_classify_000024_successor_with_authoritative():
    """000024 successor with authoritative exchange evidence => high + SQL."""
    info = {"code": "000024", "name": "招商地产", "market": "SZ", "list_date": "19910101", "is_active": 1}
    kline = {"kline_count": 3, "kline_min_date": "2026-05-18", "kline_max_date": "2026-06-15"}
    tencent = _tencent(name="招商地产", market="SZ", status="normal")
    auth = {"000024": {
        "source_authority": "exchange",
        "source_url": "https://www.szse.cn/example",
        "source_date": "2015-12-30",
        "source_sha256": "b" * 64,
        "evidence_excerpt": "招商地产 absorbed into 招商蛇口 (001979)",
        "successor": "001979",
    }}
    cat, conf, _ = classify_with_evidence(
        "000024", info, kline, tencent, "KEEP_HISTORICAL_INACTIVE", "high", authoritative=auth,
    )
    assert cat == "MAP_TO_SUCCESSOR_KEEP_HISTORY"
    assert conf == "high"


def test_r2_1_validate_label_only_rejected():
    """R2-1: label-only evidence {source_authority} must be rejected (all other
    required fields empty) and must NOT grant high or produce SQL."""
    auth = {"000024": {"source_authority": "exchange"}}
    valid, errors = validate_authoritative_evidence("000024", auth["000024"], "001979")
    assert not valid
    assert any("source_url" in e for e in errors)
    assert any("source_sha256" in e for e in errors)
    info = {"code": "000024", "name": "招商地产", "market": "SZ", "list_date": "19910101", "is_active": 1}
    kline = {"kline_count": 3, "kline_min_date": "2026-05-18", "kline_max_date": "2026-06-15"}
    tencent = _tencent(name="招商地产", market="SZ", status="normal")
    cat, conf, _ = classify_with_evidence(
        "000024", info, kline, tencent, "KEEP_HISTORICAL_INACTIVE", "high", authoritative=auth,
    )
    assert cat == "UNKNOWN_MANUAL_REVIEW"
    assert conf != "high"


def test_r2_1_validate_empty_fields_rejected():
    """R2-1: evidence with all fields present but empty must be rejected."""
    auth = {"000024": {
        "source_authority": "exchange",
        "source_url": "",
        "source_date": "",
        "source_sha256": "",
        "evidence_excerpt": "",
        "successor": "001979",
    }}
    valid, errors = validate_authoritative_evidence("000024", auth["000024"], "001979")
    assert not valid
    assert len(errors) >= 4


def test_r2_1_validate_bad_sha_rejected():
    """R2-1: a non-64-hex source_sha256 must be rejected even when everything
    else looks valid."""
    auth = {"000024": {
        "source_authority": "exchange",
        "source_url": "https://www.szse.cn/example",
        "source_date": "2015-12-30",
        "source_sha256": "zzz-not-a-sha",
        "evidence_excerpt": "招商地产 absorbed into 招商蛇口 (001979)",
        "successor": "001979",
    }}
    valid, errors = validate_authoritative_evidence("000024", auth["000024"], "001979")
    assert not valid
    assert any("source_sha256" in e for e in errors)


def test_r2_1_validate_fake_authority_rejected():
    """R2-1: a fake/unknown source_authority (not exchange/regulator/company
    disclosure) must be rejected even with a full payload."""
    auth = {"000024": {
        "source_authority": "random_blog",
        "source_url": "https://example.com/post",
        "source_date": "2015-12-30",
        "source_sha256": "c" * 64,
        "evidence_excerpt": "招商地产 absorbed into 招商蛇口 (001979)",
        "successor": "001979",
    }}
    valid, errors = validate_authoritative_evidence("000024", auth["000024"], "001979")
    assert not valid
    assert any("source_authority" in e for e in errors)


def test_r2_1_validate_wrong_successor_rejected():
    """R2-1: evidence whose structured successor does not match the target
    code -> successor relation must be rejected (binding mismatch)."""
    auth = {"000024": {
        "source_authority": "exchange",
        "source_url": "https://www.szse.cn/example",
        "source_date": "2015-12-30",
        "source_sha256": "d" * 64,
        "evidence_excerpt": "招商地产 absorbed into 招商蛇口 (001979)",
        "successor": "600333",
    }}
    valid, errors = validate_authoritative_evidence("000024", auth["000024"], "001979")
    assert not valid
    assert any("successor" in e for e in errors)


def test_r2_1_validate_http_url_rejected():
    """R2-1: a non-HTTPS source_url must be rejected."""
    auth = {"000024": {
        "source_authority": "exchange",
        "source_url": "http://www.szse.cn/example",
        "source_date": "2015-12-30",
        "source_sha256": "e" * 64,
        "evidence_excerpt": "招商地产 absorbed into 招商蛇口 (001979)",
        "successor": "001979",
    }}
    valid, errors = validate_authoritative_evidence("000024", auth["000024"], "001979")
    assert not valid
    assert any("source_url" in e for e in errors)


@pytest.mark.parametrize("bad_url", [
    "https://",
    "https:///path",
    "https://?x=1",
    "https:// ex ample.com/x",
    "https://user:pass@www.szse.cn/x",
    "https://www.szse.cn/with space",
])
def test_r3_1_validate_structurally_invalid_https_urls_rejected(bad_url):
    """R3-1: HTTPS scheme alone is not enough. URLs without a hostname,
    with userinfo, or containing whitespace are structurally invalid and must
    be rejected (degrade to manual, no SQL)."""
    auth = {"000024": {
        "source_authority": "exchange",
        "source_url": bad_url,
        "source_date": "2015-12-30",
        "source_sha256": "a" * 64,
        "evidence_excerpt": "招商地产 absorbed into 招商蛇口 (001979)",
        "successor": "001979",
    }}
    valid, errors = validate_authoritative_evidence("000024", auth["000024"], "001979")
    assert not valid
    assert any("source_url" in e for e in errors)


def test_r3_1_validate_https_scheme_only_url_rejected():
    """R3-1: 'https://' has no hostname/resource and must NOT pass the gate."""
    auth = {"000024": {
        "source_authority": "exchange",
        "source_url": "https://",
        "source_date": "2015-12-30",
        "source_sha256": "a" * 64,
        "evidence_excerpt": "招商地产 absorbed into 招商蛇口 (001979)",
        "successor": "001979",
    }}
    valid, errors = validate_authoritative_evidence("000024", auth["000024"], "001979")
    assert not valid
    assert any("source_url" in e for e in errors)


@pytest.mark.parametrize("bad_url", [
    "https://example.com:abc/path",
    "https://example.com:0/path",
    "https://example.com:65536/path",
    "https://example.com:99999/path",
])
def test_r4_1_malformed_or_out_of_range_ports_rejected_and_no_sql(bad_url):
    """R4-1: malformed, zero, and out-of-range ports cannot grant high
    confidence or produce stock_info UPDATE SQL."""
    evidence = {"000024": {
        "source_authority": "exchange",
        "source_url": bad_url,
        "source_date": "2015-12-30",
        "source_sha256": "a" * 64,
        "evidence_excerpt": "Merchants Property absorbed into successor 001979",
        "successor": "001979",
    }}
    valid, errors = validate_authoritative_evidence(
        "000024", evidence["000024"], "001979"
    )
    assert not valid
    assert any("source_url" in error for error in errors)

    info = {"code": "000024", "name": "Merchants Property", "market": "SZ",
            "list_date": "19910101", "is_active": 1}
    kline = {"kline_count": 10, "kline_min_date": "20150101",
             "kline_max_date": "20151230"}
    tencent = _tencent(name="Merchants Property", market="SZ", status="normal")
    category, confidence, reasoning = classify_with_evidence(
        "000024", info, kline, tencent, "KEEP_HISTORICAL_INACTIVE",
        "high", authoritative=evidence,
    )
    assert category == "UNKNOWN_MANUAL_REVIEW"
    assert confidence != "high"
    assert "Failed R2-1 hard gate" in reasoning
    forward, rollback = generate_sql([{
        "code": "000024",
        "category": category,
        "confidence": confidence,
        "reasoning": reasoning,
        "successor": "001979",
    }])
    assert "UPDATE stock_info" not in forward
    assert "UPDATE stock_info" not in rollback


@pytest.mark.parametrize("good_url", [
    "https://example.com:443/path",
    "https://example.com:8443/path",
])
def test_r4_1_valid_https_ports_pass(good_url):
    """R4-1 control: syntactically valid HTTPS ports remain accepted."""
    auth = {
        "source_authority": "exchange",
        "source_url": good_url,
        "source_date": "2015-12-30",
        "source_sha256": "a" * 64,
        "evidence_excerpt": "Merchants Property absorbed into successor 001979",
        "successor": "001979",
    }
    valid, errors = validate_authoritative_evidence("000024", auth, "001979")
    assert valid
    assert errors == []


def test_r3_1_classify_with_structurally_invalid_url_downgrades():
    """R3-1: structurally invalid HTTPS URL must downgrade the code below high
    (the URL cannot locate an authoritative page), so no SQL can be generated
    for it (UNKNOWN_MANUAL_REVIEW is not a high/SQL-producing category)."""
    evidence = {"000024": {
        "source_authority": "exchange",
        "source_url": "https://",
        "source_date": "2015-12-30",
        "source_sha256": "a" * 64,
        "evidence_excerpt": "招商地产 absorbed into 招商蛇口 (001979)",
        "successor": "001979",
    }}
    info = {"code": "000024", "name": "招商地产", "market": "SZ",
            "list_date": "19910101", "is_active": 1}
    kline = {"kline_count": 10, "kline_min_date": "20150101", "kline_max_date": "20151230"}
    tencent = _tencent(name="招商地产", market="SZ", status="normal")
    outcome = classify_with_evidence(
        "000024", info, kline, tencent, "KEEP_HISTORICAL_INACTIVE", "high", authoritative=evidence,
    )
    assert outcome[0] == "UNKNOWN_MANUAL_REVIEW"
    assert outcome[1] != "high"
    assert "Failed R2-1 hard gate" in outcome[2]


def test_r2_1_validate_invalid_date_rejected():
    """R2-1: a non-ISO source_date must be rejected."""
    auth = {"000024": {
        "source_authority": "exchange",
        "source_url": "https://www.szse.cn/example",
        "source_date": "2015/12/30",
        "source_sha256": "f" * 64,
        "evidence_excerpt": "招商地产 absorbed into 招商蛇口 (001979)",
        "successor": "001979",
    }}
    valid, errors = validate_authoritative_evidence("000024", auth["000024"], "001979")
    assert not valid
    assert any("source_date" in e for e in errors)


def test_r2_1_validate_ok():
    """R2-1: a fully valid authoritative record passes the hard gate."""
    auth = {"000024": {
        "source_authority": "exchange",
        "source_url": "https://www.szse.cn/example",
        "source_date": "2015-12-30",
        "source_sha256": "b" * 64,
        "evidence_excerpt": "招商地产 absorbed into 招商蛇口 (001979)",
        "successor": "001979",
    }}
    valid, errors = validate_authoritative_evidence("000024", auth["000024"], "001979")
    assert valid
    assert errors == []


def test_classify_xxx_quarantine():
    """XXX should be QUARANTINE_TEST_RECORD at low confidence (formal DB only)."""
    info = {"code": "XXX", "name": "TEST", "market": "SZ", "list_date": "20260101", "is_active": 1}
    kline = {"kline_count": 0, "kline_min_date": "", "kline_max_date": ""}
    cat, conf, _ = classify_with_evidence("XXX", info, kline, None, "QUARANTINE_TEST_RECORD", "high")
    assert cat == "QUARANTINE_TEST_RECORD"
    assert conf == "low"


def test_classify_no_evidence():
    """Code with no evidence should be low confidence KEEP_HISTORICAL_INACTIVE."""
    cat, conf, _ = classify_with_evidence("999999", None, None, None, None, None)
    assert cat == "KEEP_HISTORICAL_INACTIVE"
    assert conf == "low"


def test_generate_sql_high_only():
    """Only high-confidence items generate SQL; medium (Tencent-only) items excluded."""
    classified = [
        {"code": "000013", "category": "KEEP_HISTORICAL_INACTIVE", "confidence": "high", "reasoning": "delisted (2 external sources)"},
        {"code": "001235", "category": "UNKNOWN_MANUAL_REVIEW", "confidence": "medium", "reasoning": "needs review"},
        {"code": "920808", "category": "CORRECT_MARKET_LABEL", "confidence": "medium", "reasoning": "Tencent only"},
        {"code": "000024", "category": "MAP_TO_SUCCESSOR_KEEP_HISTORY", "confidence": "high", "reasoning": "successor",
         "successor": "001979"},
    ]
    forward, rollback = generate_sql(classified)
    fwd_sql_lines = [l for l in forward.split("\n") if l.strip().startswith("UPDATE")]
    rbk_sql_lines = [l for l in rollback.split("\n") if l.strip().startswith("UPDATE")]
    assert len(fwd_sql_lines) == 2  # 000013, 000024 only (920808 medium excluded)
    assert len(rbk_sql_lines) == 2
    assert "UPDATE stock_info" in forward
    assert "SET is_active = 0" in forward
    assert "SET is_active = 1" in rollback
    assert "SET market = '北交所'" not in forward  # 920808 is medium => no market SQL
    assert "guard" in forward
    assert "No DELETE/DROP" in forward


def test_build_evidence_db_only():
    """Evidence with only DB source should have current_db authority."""
    info = {"code": "000001", "name": "平安银行", "market": "SZ", "list_date": "19910101", "is_active": 1}
    kline = {"kline_count": 100, "kline_min_date": "2020-01-01", "kline_max_date": "2026-07-01"}
    e = build_evidence("000001", info, kline, None)
    assert e["source_authority"] == "current_db"
    assert "平安银行" in e["evidence_excerpt"]
    assert _has_required_fields(e)


def test_build_evidence_with_tencent():
    """Evidence with DB + Tencent should carry both authorities + url/sha."""
    info = {"code": "000013", "name": "*ST石化A", "market": "SZ", "list_date": "19910101", "is_active": 1}
    kline = {"kline_count": 13, "kline_min_date": "2026-06-29", "kline_max_date": "2026-07-17"}
    tencent = _tencent(name="*ST石化A", market="SZ", status="delisted")
    e = build_evidence("000013", info, kline, tencent)
    assert "current_db" in e["source_authority"]
    assert "secondary" in e["source_authority"]
    assert "tencent" in e["evidence_excerpt"].lower()
    assert tencent["source_url"] in e["source_url"]
    assert tencent["response_sha256"] in e["source_sha256"]
    assert _has_required_fields(e)


def test_crawl_budget():
    """CrawlBudget tracks requests and fuses sources on failure."""
    b = CrawlBudget(10, 60, 3)
    assert b.can_request("tencent")
    b.record("tencent", "test", "OK")
    assert b.requests == 1
    for _ in range(3):
        b.record("tencent", "test", "ERROR", error="fail")
    assert "tencent" in b.fused_sources
    assert not b.can_request("tencent")
    b2 = CrawlBudget(2, 60, 3)
    b2.record("test", "a", "OK")
    b2.record("test", "b", "OK")
    assert not b2.can_request("test")


def test_investigate_001235(memory_db):
    """investigate_001235 should return structured results without touching klines."""
    budget = CrawlBudget(5, 60, 3)
    with patch("audit_security_identity_evidence.query_tencent_batch", return_value={
        "001235": _tencent(name="无效里得", market="SZ", status="unlisted")
    }):
        result = investigate_001235(memory_db, budget)
        assert result["code"] == "001235"
        assert result["formal_db_facts"]["name"] == "无效里得"
        assert result["tencent_facts"]["status"] == "unlisted"
        assert result["conclusion"] == "UNKNOWN_MANUAL_REVIEW"
        assert result["classification_analysis"]["is_active"] is True
        assert result["classification_analysis"]["has_klines"] is True
        assert result["classification_analysis"]["flat_klines"] is True
        assert "no automatic action" in result["recommendation"]


def test_investigate_001235_network_failure(memory_db):
    """Network failure should not crash the 001235 investigation."""
    budget = CrawlBudget(5, 60, 3)
    with patch("audit_security_identity_evidence.query_tencent_batch", side_effect=RuntimeError("timeout")):
        result = investigate_001235(memory_db, budget)
        assert result["conclusion"] == "UNKNOWN_MANUAL_REVIEW"
        assert budget.failures >= 1


def test_generate_sql_no_delete():
    """Generated SQL must not contain DELETE/DROP/INSERT/ALTER."""
    classified = [
        {"code": "000013", "category": "KEEP_HISTORICAL_INACTIVE", "confidence": "high", "reasoning": "test"},
    ]
    forward, rollback = generate_sql(classified)
    for line in forward.split("\n"):
        stripped = line.strip()
        if stripped.startswith("--"):
            continue
        for stmt in ["DELETE", "DROP", "INSERT", "ALTER", "VACUUM", "REINDEX"]:
            assert stmt not in stripped.upper(), f"Found {stmt} in SQL: {line}"


def test_generate_sql_empty():
    """No high-confidence items should produce empty SQL."""
    classified = [
        {"code": "001235", "category": "UNKNOWN_MANUAL_REVIEW", "confidence": "medium", "reasoning": "needs review"},
        {"code": "999999", "category": "KEEP_HISTORICAL_INACTIVE", "confidence": "low", "reasoning": "no evidence"},
    ]
    forward, rollback = generate_sql(classified)
    fwd_sql = [l for l in forward.split("\n") if l.strip().startswith("UPDATE")]
    rbk_sql = [l for l in rollback.split("\n") if l.strip().startswith("UPDATE")]
    assert len(fwd_sql) == 0
    assert len(rbk_sql) == 0
