from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

import pytest

from data_layer.valuation_dual_read import (
    DualReadConfig,
    DualReadMismatchError,
    DualReadSafetyError,
    DualReadSchemaError,
    DualReadTimeoutError,
    ReadOnlySQLite,
    ValuationDualReadObserver,
    ValuationQuery,
    compare_rows,
    observer_from_config,
)

VALUATION_DDL = """
CREATE TABLE stock_valuation (
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    pe_ttm REAL DEFAULT 0,
    pb_mrq REAL DEFAULT 0,
    ps_ttm REAL DEFAULT 0,
    market_cap REAL DEFAULT 0,
    total_shares REAL DEFAULT 0,
    dividend_yield REAL,
    PRIMARY KEY(code,date)
)
"""


def make_db(path: Path, rows=(), ddl: str = VALUATION_DDL) -> None:
    connection = sqlite3.connect(path)
    connection.execute(ddl)
    connection.executemany(
        "INSERT INTO stock_valuation "
        "(code,date,pe_ttm,pb_mrq,ps_ttm,market_cap,total_shares,dividend_yield) "
        "VALUES (?,?,?,?,?,?,?,?)",
        rows,
    )
    connection.commit()
    connection.close()


def row(code="920000", date="2026-08-01", pe=10.0, pb=1.0, ps=2.0):
    return (code, date, pe, pb, ps, 100.0, 10.0, 0.5)


class DictConfig:
    def __init__(self, values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def full_config(
    shadow: Path,
    evidence_dir: Path | None = None,
    policy="open",
    max_detail_items: int = 50,
) -> DualReadConfig:
    return DualReadConfig(
        enabled=True,
        mode="full",
        failure_policy=policy,
        timeout_seconds=1.0,
        evidence_dir=evidence_dir,
        expected_shadow_sha256=file_sha(shadow),
        max_detail_items=max_detail_items,
    )


def test_disabled_observer_touches_no_database(tmp_path):
    observer = ValuationDualReadObserver(
        tmp_path / "missing-formal.db",
        tmp_path / "missing-shadow.db",
        DualReadConfig(),
    )
    result = observer.compare(ValuationQuery("latest", "920000", as_of_date="2026-08-01"))
    assert result.status == "DISABLED"
    assert result.compared is False
    assert list(tmp_path.iterdir()) == []


def test_config_factory_is_default_off_and_requires_shadow_when_enabled(tmp_path):
    assert observer_from_config(tmp_path / "formal.db", DictConfig({})) is None
    assert observer_from_config(
        tmp_path / "formal.db", DictConfig({"data.valuation_dual_read.enabled": "false"})
    ) is None
    with pytest.raises(ValueError, match="invalid explicit boolean"):
        observer_from_config(
            tmp_path / "formal.db", DictConfig({"data.valuation_dual_read.enabled": "maybe"})
        )
    with pytest.raises(ValueError, match="requires shadow_db"):
        observer_from_config(
            tmp_path / "formal.db",
            DictConfig({"data.valuation_dual_read.enabled": True}),
        )


def test_range_latest_and_pit_match_with_date_normalization_and_no_sidecars(tmp_path):
    formal = tmp_path / "formal.db"
    shadow = tmp_path / "shadow.db"
    make_db(formal, [row(date="20260801"), row(date="20260802", pe=11.0)])
    make_db(shadow, [row(date="2026-08-01"), row(date="2026-08-02", pe=11.0)])
    observer = ValuationDualReadObserver(formal, shadow, full_config(shadow))
    assert observer.preflight()["status"] == "PASS_DUAL_READ_PREFLIGHT"

    results = [
        observer.compare(ValuationQuery("range", "920000", "2026-08-01", "2026-08-02")),
        observer.compare(ValuationQuery("latest", "920000", as_of_date="2026-08-02")),
        observer.compare(ValuationQuery("pit", "920000", as_of_date="2026-08-01")),
    ]

    assert [result.status for result in results] == ["MATCH", "MATCH", "MATCH"]
    assert observer.summary(results)["ready"] is True
    assert not Path(str(formal) + "-wal").exists()
    assert not Path(str(formal) + "-shm").exists()
    assert not Path(str(shadow) + "-wal").exists()
    assert not Path(str(shadow) + "-shm").exists()


def test_mismatch_classes_cover_missing_extra_value_duplicate_and_nonfinite():
    formal = [
        dict(zip(("code", "date", "pe_ttm", "pb_mrq", "ps_ttm", "market_cap", "total_shares", "dividend_yield"), row(date="20260801"))),
        dict(zip(("code", "date", "pe_ttm", "pb_mrq", "ps_ttm", "market_cap", "total_shares", "dividend_yield"), row(date="2026-08-01"))),
        dict(zip(("code", "date", "pe_ttm", "pb_mrq", "ps_ttm", "market_cap", "total_shares", "dividend_yield"), row(date="2026-08-02"))),
    ]
    shadow = [
        dict(zip(("code", "date", "pe_ttm", "pb_mrq", "ps_ttm", "market_cap", "total_shares", "dividend_yield"), row(date="2026-08-01", pe=99.0))),
        dict(zip(("code", "date", "pe_ttm", "pb_mrq", "ps_ttm", "market_cap", "total_shares", "dividend_yield"), row(date="2026-08-03", pe=float("inf")))),
    ]
    classes, detail = compare_rows(formal, shadow)
    assert set(classes) == {
        "duplicate_key",
        "missing_in_shadow",
        "extra_in_shadow",
        "value_mismatch",
        "nonfinite_value",
    }
    assert detail["value_mismatches"][0]["differences"]["pe_ttm"] == {
        "formal": 10.0,
        "shadow": 99.0,
    }
    assert detail["nonfinite_value_count"] == 1
    assert len(detail["nonfinite_value_digest"]) == 64


def test_nonfinite_evidence_is_strict_json(tmp_path):
    formal = tmp_path / "formal.db"
    shadow = tmp_path / "shadow.db"
    evidence = tmp_path / "evidence"
    make_db(formal, [row(pe=10.0)])
    make_db(shadow, [row(pe=float("inf"))])
    observer = ValuationDualReadObserver(shadow_db=shadow, formal_db=formal, config=full_config(shadow, evidence))
    observer.preflight()
    result = observer.compare(ValuationQuery("latest", "920000", as_of_date="2026-08-01"))
    text = Path(result.evidence_path).read_text()
    assert '"+Infinity"' in text
    assert ": Infinity" not in text
    assert json.loads(text)["status"] == "MISMATCH"


def test_fail_open_returns_mismatch_and_writes_no_clobber_evidence(tmp_path):
    formal = tmp_path / "formal.db"
    shadow = tmp_path / "shadow.db"
    evidence = tmp_path / "evidence"
    make_db(formal, [row(pe=10.0)])
    make_db(shadow, [row(pe=20.0)])
    observer = ValuationDualReadObserver(formal, shadow, full_config(shadow, evidence))
    observer.preflight()

    result = observer.compare(ValuationQuery("latest", "920000", as_of_date="2026-08-01"))

    assert result.status == "MISMATCH"
    assert result.mismatch_classes == ["value_mismatch"]
    evidence_path = Path(result.evidence_path)
    assert evidence_path.is_file()
    payload = json.loads(evidence_path.read_text())
    assert payload["status"] == "MISMATCH"
    assert not list(evidence.glob("*.tmp"))


def test_fail_closed_raises_after_writing_evidence(tmp_path):
    formal = tmp_path / "formal.db"
    shadow = tmp_path / "shadow.db"
    evidence = tmp_path / "evidence"
    make_db(formal, [row(pe=10.0)])
    make_db(shadow, [row(pe=20.0)])
    observer = ValuationDualReadObserver(formal, shadow, full_config(shadow, evidence, policy="closed"))
    observer.preflight()

    with pytest.raises(DualReadMismatchError, match="value_mismatch"):
        observer.compare(ValuationQuery("latest", "920000", as_of_date="2026-08-01"))
    assert len(list(evidence.glob("dual-read-*.json"))) == 1


def test_enabled_request_before_preflight_fails_fast_without_hash(tmp_path):
    formal = tmp_path / "formal.db"
    shadow = tmp_path / "shadow.db"
    make_db(formal, [row()])
    make_db(shadow, [row()])
    observer = ValuationDualReadObserver(formal, shadow, full_config(shadow))
    result = observer.compare(ValuationQuery("latest", "920000", as_of_date="2026-08-01"))
    assert result.status == "ERROR"
    assert result.mismatch_classes == ["unsafe_input"]
    assert "preflight has not completed" in result.detail["error"]
    assert result.elapsed_ms < 100


def test_preflight_timeout_is_validated_and_interrupts_before_verification(tmp_path):
    with pytest.raises(ValueError, match="preflight_timeout_seconds"):
        DualReadConfig(preflight_timeout_seconds=0)

    formal = tmp_path / "formal.db"
    shadow = tmp_path / "shadow.db"
    make_db(formal, [row()])
    make_db(shadow, [row()])
    observer = ValuationDualReadObserver(
        formal,
        shadow,
        DualReadConfig(
            enabled=True,
            mode="full",
            expected_shadow_sha256=file_sha(shadow),
            preflight_timeout_seconds=1e-12,
        ),
    )

    with pytest.raises(DualReadTimeoutError, match="preflight exceeded"):
        observer.preflight()
    result = observer.compare(ValuationQuery("latest", "920000", as_of_date="2026-08-01"))
    assert result.status == "ERROR"
    assert result.mismatch_classes == ["unsafe_input"]


def test_schema_drift_and_nonempty_wal_are_rejected_by_preflight(tmp_path):
    formal = tmp_path / "formal.db"
    bad_schema = tmp_path / "bad-schema.db"
    wal_shadow = tmp_path / "wal-shadow.db"
    make_db(formal, [row()])
    make_db(
        bad_schema,
        [row()],
        ddl=VALUATION_DDL.replace("pe_ttm REAL DEFAULT 0", "pe_ttm REAL"),
    )
    make_db(wal_shadow, [row()])
    Path(str(wal_shadow) + "-wal").write_bytes(b"not-empty")
    query = ValuationQuery("latest", "920000", as_of_date="2026-08-01")

    schema_observer = ValuationDualReadObserver(formal, bad_schema, full_config(bad_schema))
    wal_observer = ValuationDualReadObserver(formal, wal_shadow, full_config(wal_shadow))

    with pytest.raises(DualReadSchemaError, match="schema mismatch"):
        schema_observer.preflight()
    with pytest.raises(DualReadSafetyError, match="non-empty WAL"):
        wal_observer.preflight()


def test_readonly_adapter_rejects_symlink(tmp_path):
    database = tmp_path / "database.db"
    link = tmp_path / "link.db"
    make_db(database, [row()])
    link.symlink_to(database)
    with pytest.raises(DualReadSafetyError, match="symlink"):
        ReadOnlySQLite(link)


def test_stable_sampling_is_deterministic_and_summary_counts_skips(tmp_path):
    formal = tmp_path / "formal.db"
    shadow = tmp_path / "shadow.db"
    make_db(formal, [row()])
    make_db(shadow, [row()])
    config = DualReadConfig(
        enabled=True,
        mode="sample",
        sample_modulus=1000,
        sample_buckets=(0,),
        expected_shadow_sha256=file_sha(shadow),
    )
    observer = ValuationDualReadObserver(formal, shadow, config)
    query = ValuationQuery("latest", "920000", as_of_date="2026-08-01")

    decisions = [observer.should_compare(query) for _ in range(20)]
    assert len(set(decisions)) == 1
    observer.preflight()
    result = observer.compare(query)
    assert result.status in ("MATCH", "SKIPPED")
    summary = observer.summary([result])
    assert summary["total"] == 1
    assert summary["compared"] == int(result.compared)


def test_timeout_interrupts_flow_and_fail_closed_raises(tmp_path, monkeypatch):
    formal = tmp_path / "formal.db"
    shadow = tmp_path / "shadow.db"
    make_db(formal, [row()])
    make_db(shadow, [row()])
    config = DualReadConfig(
        enabled=True,
        mode="full",
        failure_policy="closed",
        timeout_seconds=0.01,
        expected_shadow_sha256=file_sha(shadow),
    )
    observer = ValuationDualReadObserver(formal, shadow, config)
    observer.preflight()
    original = observer.formal.query

    def slow_query(query, deadline=None):
        time.sleep(0.02)
        return original(query, deadline=None)

    monkeypatch.setattr(observer.formal, "query", slow_query)
    with pytest.raises(DualReadTimeoutError, match="TIMEOUT"):
        observer.compare(ValuationQuery("latest", "920000", as_of_date="2026-08-01"))


def test_invalid_query_inputs_fail_before_database_access():
    with pytest.raises(ValueError, match="invalid security code"):
        ValuationQuery("latest", "../bad", as_of_date="2026-08-01")
    with pytest.raises(ValueError, match="invalid date"):
        ValuationQuery("latest", "920000", as_of_date="2026-99-99")
    with pytest.raises(ValueError, match="exceeds"):
        ValuationQuery("range", "920000", "2026-08-02", "2026-08-01")


def test_enabled_observer_rejects_same_file_and_unpinned_or_wrong_shadow(tmp_path):
    formal = tmp_path / "formal.db"
    shadow = tmp_path / "shadow.db"
    make_db(formal, [row()])
    make_db(shadow, [row()])
    with pytest.raises(DualReadSafetyError, match="different files"):
        ValuationDualReadObserver(formal, formal, DualReadConfig())
    with pytest.raises(ValueError, match="expected_shadow_sha256"):
        ValuationDualReadObserver(formal, shadow, DualReadConfig(enabled=True, mode="full"))
    observer = ValuationDualReadObserver(
        formal,
        shadow,
        DualReadConfig(enabled=True, mode="full", expected_shadow_sha256="0" * 64),
    )
    with pytest.raises(DualReadSafetyError, match="shadow SHA mismatch"):
        observer.preflight()


def test_shadow_state_change_after_first_hash_is_rejected(tmp_path):
    formal = tmp_path / "formal.db"
    shadow = tmp_path / "shadow.db"
    make_db(formal, [row()])
    make_db(shadow, [row()])
    observer = ValuationDualReadObserver(formal, shadow, full_config(shadow))
    query = ValuationQuery("latest", "920000", as_of_date="2026-08-01")
    observer.preflight()
    assert observer.compare(query).status == "MATCH"
    stat = shadow.stat()
    shadow.touch()
    assert shadow.stat().st_mtime_ns != stat.st_mtime_ns
    result = observer.compare(query)
    assert result.status == "ERROR"
    assert result.mismatch_classes == ["unsafe_input"]
    assert "changed after preflight" in result.detail["error"]


def test_large_mismatch_detail_is_bounded_with_count_and_digest():
    formal = []
    shadow = [
        dict(zip(("code", "date", "pe_ttm", "pb_mrq", "ps_ttm", "market_cap", "total_shares", "dividend_yield"), row(date=f"2026-08-{day:02d}")))
        for day in range(1, 11)
    ]
    classes, detail = compare_rows(formal, shadow, max_detail_items=3)
    assert classes == ["extra_in_shadow"]
    assert detail["extra_in_shadow_count"] == 10
    assert len(detail["extra_in_shadow"]) == 3
    assert len(detail["extra_in_shadow_digest"]) == 64


def test_config_factory_builds_explicit_full_observer(tmp_path):
    formal = tmp_path / "formal.db"
    shadow = tmp_path / "shadow.db"
    make_db(formal, [row()])
    make_db(shadow, [row()])
    config = DictConfig({
        "data.valuation_dual_read.enabled": True,
        "data.valuation_dual_read.shadow_db": str(shadow),
        "data.valuation_dual_read.mode": "full",
        "data.valuation_dual_read.failure_policy": "open",
        "data.valuation_dual_read.sample_buckets": "0,1",
        "data.valuation_dual_read.shadow_sha256": file_sha(shadow),
    })
    observer = observer_from_config(formal, config)
    assert observer is not None
    assert observer.config.enabled is True
    assert observer.config.mode == "full"
    assert observer.config.sample_buckets == (0, 1)
    assert observer.preflight()["status"] == "PASS_DUAL_READ_PREFLIGHT"
