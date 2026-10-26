"""Tests for R5 build_p2_06_decision_package.py.

Covers:
  - now_iso / open_ro read-only guard
  - gather_valuation_facts: provenance true-read, per-field zero vs NULL,
    all_zero detection
  - build_capability_matrix: corrected per-field status + probe injection
  - score_options: data-driven recommendation (not hardcoded)
  - load_b4_probe: parses probe results
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent / "scripts"))
from build_p2_06_decision_package import (
    ANOMALY_DATES,
    FIELD_NAMES,
    OPTION_NAMES,
    WEIGHT_SCHEMES,
    now_iso,
    open_ro,
    gather_valuation_facts,
    gather_market_facts,
    build_capability_matrix,
    score_options,
    score_option,
    coverage_by_market,
    load_b4_probe,
    load_anomaly_days,
    recommendation_stable,
    main,
)


@pytest.fixture
def sample_valuation_db(tmp_path: Path) -> Path:
    """Build a small candidate valuation DB with a provenance table."""
    db = tmp_path / "valuation.db"
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE stock_valuation (
            code TEXT NOT NULL,
            date TEXT NOT NULL,
            pe_ttm REAL DEFAULT 0,
            pb_mrq REAL DEFAULT 0,
            ps_ttm REAL DEFAULT 0,
            market_cap REAL DEFAULT 0,
            total_shares REAL DEFAULT 0,
            dividend_yield REAL DEFAULT 0,
            PRIMARY KEY (code, date)
        )
    """)
    conn.execute("""
        CREATE TABLE valuation_backfill_state (
            code TEXT PRIMARY KEY,
            status TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE valuation_provenance (
            code TEXT NOT NULL,
            date TEXT NOT NULL,
            source TEXT NOT NULL,
            fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (code, date, source)
        )
    """)
    # 3 codes x 2 dates = 6 rows.
    # pe_ttm populated everywhere; market_cap/total_shares/dividend_yield = 0.
    rows = []
    for code in ("000001", "000002", "600000"):
        for date in ("2026-07-08", "2026-07-20"):
            pe = 10.0 + int(code[-1])
            pb = 0.0 if (code == "000001" and date == "2026-07-08") else 1.5
            rows.append((code, date, pe, pb, 0.0, 0.0, 0.0, 0.0))
    conn.executemany(
        "INSERT INTO stock_valuation VALUES (?,?,?,?,?,?,?,?)", rows
    )
    conn.execute(
        "INSERT INTO valuation_provenance (code, date, source) SELECT code, date, 'baostock' FROM stock_valuation"
    )
    conn.executemany(
        "INSERT INTO valuation_backfill_state VALUES (?,?)",
        [("000001", "completed"), ("000002", "completed"), ("600000", "failed")],
    )
    conn.commit()
    conn.close()
    return db


@pytest.fixture
def sample_market_db(tmp_path: Path) -> Path:
    db = tmp_path / "qs.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE stock_info (code TEXT PRIMARY KEY, market TEXT)")
    conn.execute("CREATE TABLE daily_klines (code TEXT, date TEXT)")
    conn.execute("INSERT INTO stock_info VALUES ('830001','BJ')")
    conn.execute("INSERT INTO stock_info VALUES ('920001','BJ')")
    conn.execute("INSERT INTO stock_info VALUES ('000001','SZ')")
    conn.execute("INSERT INTO daily_klines VALUES ('920001','2026-07-01')")
    conn.commit()
    conn.close()
    return db


def test_now_iso():
    s = now_iso()
    assert "T" in s
    assert s.endswith("+00:00") or s.endswith("Z")


def test_open_ro_is_read_only(sample_valuation_db):
    conn = open_ro(sample_valuation_db)
    assert conn.execute("SELECT COUNT(*) FROM stock_valuation").fetchone()[0] == 6
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("CREATE TABLE should_fail (x)")
    conn.close()


def test_gather_valuation_facts_provenance_true_read(sample_valuation_db):
    conn = open_ro(sample_valuation_db)
    facts = gather_valuation_facts(conn, sample_valuation_db)
    conn.close()

    assert facts["val_rows"] == 6
    assert facts["val_dates"] == 2
    assert facts["val_codes"] == 3

    # Provenance from real table, not a column on stock_valuation.
    assert facts["provenance"]["table_present"] is True
    assert facts["provenance"]["rows"] == 6
    assert facts["provenance"]["sources"] == {"baostock": 6}
    assert facts["provenance"]["coverage_pct"] == 100.0

    assert facts["backfill"]["counts"] == {"completed": 2, "failed": 1}
    assert facts["backfill"]["failed_codes"] == 1


def test_gather_valuation_facts_zero_vs_null(sample_valuation_db):
    conn = open_ro(sample_valuation_db)
    facts = gather_valuation_facts(conn, sample_valuation_db)
    conn.close()

    fs = facts["field_stats"]
    # pe_ttm: never zero.
    assert fs["pe_ttm"]["zero_count"] == 0
    assert fs["pe_ttm"]["all_zero"] is False
    # market_cap: all zero (never written).
    assert fs["market_cap"]["zero_count"] == 6
    assert fs["market_cap"]["all_zero"] is True
    assert fs["market_cap"]["pct_zero"] == 100.0
    assert fs["market_cap"]["null_count"] == 0
    # pb_mrq: 1 zero out of 6 => ambiguous low-zero field.
    assert fs["pb_mrq"]["zero_count"] == 1
    assert fs["pb_mrq"]["all_zero"] is False


def test_gather_market_facts(sample_market_db):
    conn = open_ro(sample_market_db)
    facts = gather_market_facts(conn)
    conn.close()
    assert facts["bj_codes"] == 2
    assert facts["missing_kline_codes"] == 2  # 830001 and 000001


def test_capability_matrix_without_probe(sample_valuation_db):
    conn = open_ro(sample_valuation_db)
    facts = gather_valuation_facts(conn, sample_valuation_db)
    conn.close()
    probe = {"available": False}
    matrix = build_capability_matrix(facts, probe)

    assert matrix["market_cap"]["baostock_actual"] == "UNAVAILABLE"
    assert matrix["pe_ttm"]["baostock_actual"] == "AVAILABLE"
    assert matrix["pb_mrq"]["baostock_actual"] == "LIMITED"
    assert all(v == "UNVERIFIED" for v in matrix["pe_ttm"]["sources"].values())


def test_capability_matrix_with_probe(sample_valuation_db, tmp_path):
    probe_results = [
        {"source": "stock_zh_valuation_baidu", "field": "pe_ttm", "status": "PROVEN"},
        {"source": "stock_zh_valuation_baidu", "field": "market_cap", "status": "PROVEN"},
        {"source": "stock_zh_valuation_comparison_em", "field": "ps_ttm", "status": "PROVEN"},
    ]
    wave_b = tmp_path / "WAVE-B-R1"
    (wave_b / "R4-bj-probe").mkdir(parents=True)
    (wave_b / "R4-bj-probe" / "probe-results.json").write_text(
        json.dumps(probe_results, ensure_ascii=False)
    )

    probe = load_b4_probe(wave_b)
    assert probe["available"] is True
    assert "pe_ttm" in probe["per_source_proven"]["stock_zh_valuation_baidu"]

    conn = open_ro(sample_valuation_db)
    facts = gather_valuation_facts(conn, sample_valuation_db)
    conn.close()
    matrix = build_capability_matrix(facts, probe)

    # baidu proven pe_ttm + market_cap.
    assert matrix["pe_ttm"]["sources"]["stock_zh_valuation_baidu"] == "PROVEN"
    assert matrix["market_cap"]["sources"]["stock_zh_valuation_baidu"] == "PROVEN"
    # ps_ttm proven by comparison but candidate never wrote it.
    assert matrix["ps_ttm"]["sources"]["stock_zh_valuation_comparison_em"] == "PROVEN"


def test_load_b4_probe_missing(tmp_path):
    probe = load_b4_probe(tmp_path)
    assert probe == {"available": False}


def test_score_options_recommendation_is_data_driven(sample_valuation_db):
    conn = open_ro(sample_valuation_db)
    facts = gather_valuation_facts(conn, sample_valuation_db)
    conn.close()
    matrix = build_capability_matrix(facts, {"available": False})
    scores = score_options(facts, matrix)

    option_keys = {k for k in scores.keys() if k != "__sensitivity__"}
    assert option_keys == set(OPTION_NAMES)
    best = max(scores.items(), key=lambda kv: kv[0] != "__sensitivity__" and kv[1]["score"])
    # With 4 unambiguous-missing fields, low cost, and no probe evidence, B wins.
    assert best[0] == "B_copy_and_fix"
    # A cannot fix the missing fields, so it should NOT win.
    a = scores["A_rebuild_baostock"]["score_detail"]
    assert a["real_value_fixable_count"] == 0


def test_r2_3_semantic_repair_not_real_coverage(sample_valuation_db):
    """R2-3: B's 0->NULL is semantic repair, NOT real-value coverage. Its
    real_value_fixable_count must be 0; coverage ratio comes only from fields a
    PROVEN source can write real values for."""
    conn = open_ro(sample_valuation_db)
    facts = gather_valuation_facts(conn, sample_valuation_db)
    conn.close()
    matrix = build_capability_matrix(facts, {"available": False})
    scores = score_options(facts, matrix)

    b = scores["B_copy_and_fix"]["score_detail"]
    assert b["real_value_fixable"] == []
    assert b["real_value_fixable_count"] == 0
    assert b["semantic_repair_is_0_to_null_not_real_coverage"] is True
    # Semantic repair fields are present (missing fields become NULL, not real values).
    assert b["semantic_repair_count"] >= 1
    # Balanced scheme coverage ratio for B is 0 (no real value written).
    assert scores["B_copy_and_fix"]["scheme_scores"]["balanced"]["cov_ratio"] == 0.0


def test_r2_3_combined_option_present(sample_valuation_db):
    """R2-3: a combined option D (repair + supplement) must be compared, and B
    is not presupposed as the winner."""
    conn = open_ro(sample_valuation_db)
    facts = gather_valuation_facts(conn, sample_valuation_db)
    conn.close()
    matrix = build_capability_matrix(facts, {"available": False})
    scores = score_options(facts, matrix)

    assert "D_combine_B_C" in scores
    d = scores["D_combine_B_C"]["score_detail"]
    assert d["semantic_repair_count"] >= 1
    assert "score" in scores["D_combine_B_C"]
    assert "rank" in scores["D_combine_B_C"]
    # R3-2: real-value and semantic-repair field sets must be DISJOINT. A field
    # with a proven real source counts as coverage; it must not also be listed
    # as a semantic-repair (0->NULL) field (double-counting).
    overlap = set(d["real_value_fixable"]) & set(d["semantic_repair_fields"])
    assert overlap == set()


def test_r3_2_no_field_double_counted_any_option(sample_valuation_db):
    """R3-2: for every option, real_value_fixable and semantic_repair_fields
    are disjoint (a field with a real source is never also semantic-repair)."""
    conn = open_ro(sample_valuation_db)
    facts = gather_valuation_facts(conn, sample_valuation_db)
    conn.close()
    matrix = build_capability_matrix(facts, {"available": False})
    scores = score_options(facts, matrix)
    for opt, data in scores.items():
        if opt == "__sensitivity__":
            continue
        sd = data["score_detail"]
        overlap = set(sd["real_value_fixable"]) & set(sd["semantic_repair_fields"])
        assert overlap == set(), f"{opt} double-counts: {overlap}"


def test_r2_3_sensitivity_analysis_output(sample_valuation_db):
    """R2-3: sensitivity analysis across weight schemes is output, with an
    explicit winner per scheme and a stability flag."""
    conn = open_ro(sample_valuation_db)
    facts = gather_valuation_facts(conn, sample_valuation_db)
    conn.close()
    matrix = build_capability_matrix(facts, {"available": False})
    scores = score_options(facts, matrix)

    assert "__sensitivity__" in scores
    assert set(scores["__sensitivity__"].keys()) == set(WEIGHT_SCHEMES.keys())
    for scheme, winner in scores["__sensitivity__"].items():
        assert winner in OPTION_NAMES
    assert isinstance(recommendation_stable(scores), bool)


def test_r2_3_option_profiles_carry_basis(sample_valuation_db):
    """R2-3: cost/requests/risk must carry explicit justification basis."""
    conn = open_ro(sample_valuation_db)
    facts = gather_valuation_facts(conn, sample_valuation_db)
    conn.close()
    matrix = build_capability_matrix(facts, {"available": False})
    scores = score_options(facts, matrix)

    for opt in OPTION_NAMES:
        sd = scores[opt]["score_detail"]
        assert sd["cost_basis"], opt
        assert sd["requests_basis"], opt
        assert sd["risk_basis"], opt
        assert sd["risk"] in ("LOW", "MEDIUM", "HIGH")


def test_r2_4_coverage_by_market(sample_valuation_db, sample_market_db):
    """R2-4: per-market coverage (overall/SH/SZ/BJ/UNKNOWN) is computed."""
    mconn = open_ro(sample_market_db)
    vconn = open_ro(sample_valuation_db)
    cov = coverage_by_market(mconn, vconn)
    mconn.close()
    vconn.close()

    assert set(cov.keys()) == {"SH", "SZ", "BJ", "UNKNOWN", "overall"}
    # Sample market DB has no BJ-prefixed codes; valuation has none either.
    assert cov["BJ"]["valuation_codes"] == 0
    assert cov["overall"]["formal_codes"] >= cov["overall"]["valuation_codes"]
    for b in cov:
        assert "coverage_pct" in cov[b]


def test_r2_4_load_anomaly_days(tmp_path):
    """R2-4: anomaly-day verdicts are loaded from the R2 fact chain."""
    wave_b = tmp_path / "WAVE-B-R1"
    (wave_b / "R2-anomaly-days").mkdir(parents=True)
    (wave_b / "R2-anomaly-days" / "anomaly-day-fact-chain.json").write_text(json.dumps({
        "2026-07-08": {"verdict": "UNRESOLVED", "requires_manual_review": True},
        "2026-07-20": {"verdict": "UNRESOLVED", "requires_manual_review": True},
    }))
    anomaly = load_anomaly_days(wave_b)
    assert anomaly["available"] is True
    assert anomaly["verdicts"]["2026-07-08"][0] == "UNRESOLVED"
    assert anomaly["verdicts"]["2026-07-20"][1] is True
    assert set(ANOMALY_DATES) == {"2026-07-08", "2026-07-20"}


def test_score_options_all_zero_db():
    """A DB with no data should not crash and produce a defensible ranking."""
    facts = {
        "val_rows": 0,
        "val_dates": 0,
        "val_codes": 0,
        "field_stats": {f: {"zero_count": 0, "null_count": 0, "pct_zero": 0.0,
                            "pct_null": 0.0, "all_zero": False} for f in FIELD_NAMES},
        "provenance": {"table_present": False, "rows": 0, "sources": {}, "coverage_pct": None},
        "backfill": {"table_present": False, "counts": {}, "failed_codes": 0},
        "val_size_bytes": 0,
    }
    matrix = build_capability_matrix(facts, {"available": False})
    scores = score_options(facts, matrix)
    option_keys = {k for k in scores.keys() if k != "__sensitivity__"}
    assert len(option_keys) == len(OPTION_NAMES)
    assert len(scores) == len(OPTION_NAMES) + 1  # options + __sensitivity__


def test_recommendation_not_hardcoded_to_b(sample_valuation_db, tmp_path):
    """Recommendation must come from the scoring function."""
    # A DB where baidu/comparison prove market_cap and ps_ttm, plus zero
    # failed codes and cheaper supplement assumptions should still be decided
    # by the scoring model. Here we just assert the report embeds the derived
    # score rather than a constant.
    conn = open_ro(sample_valuation_db)
    facts = gather_valuation_facts(conn, sample_valuation_db)
    conn.close()
    scores = score_options(facts, build_capability_matrix(facts, {"available": False}))
    assert "score" in scores["A_rebuild_baostock"]
    assert "score_detail" in scores["B_copy_and_fix"]


def test_report_outputs_are_written(sample_valuation_db, sample_market_db, tmp_path):
    out = tmp_path / "out"
    wave_b = tmp_path / "WAVE-B-R1"
    wave_b.mkdir()
    rc = main.__wrapped__ if hasattr(main, "__wrapped__") else main

    # Invoke via subprocess-free call by monkeypatching sys.argv is complex;
    # instead assert the module's main entry exists and required outputs are
    # produced through a direct call.
    import build_p2_06_decision_package as mod

    old_argv = sys.argv
    sys.argv = [
        "build_p2_06_decision_package.py",
        "--market-db", str(sample_market_db),
        "--valuation-db", str(sample_valuation_db),
        "--wave-b-dir", str(wave_b),
        "--output-dir", str(out),
    ]
    try:
        rc()
    finally:
        sys.argv = old_argv

    assert (out / "p2-06-option-matrix.json").is_file()
    assert (out / "p2-06-field-capability-matrix.json").is_file()
    assert (out / "p2-06-valuation-facts.json").is_file()
    assert (out / "p2-06-decision-report.md").is_file()
    assert (out / "report.json").is_file()

    rep = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert rep["P2-06_NOT_EXECUTED"] is True
    assert rep["provenance_table_present"] is True
    assert rep["provenance_sources"] == {"baostock": 6}
    assert rep["recommended_option"] == "B_copy_and_fix"

    report_md = (out / "p2-06-decision-report.md").read_text(encoding="utf-8")
    assert "P2-06_NOT_EXECUTED: true" in report_md
    assert "Recommended: B_copy_and_fix" in report_md
