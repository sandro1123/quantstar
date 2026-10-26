from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from data_layer.valuation_dual_read import (
    DualReadMismatchError,
    DualReadTimeoutError,
    ValuationQuery,
)
from data_layer.valuation_dual_read_service import (
    ValuationDualReadService,
    service_from_config,
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


class DictConfig:
    def __init__(self, values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)


def make_db(path: Path, pe: float = 10.0) -> None:
    connection = sqlite3.connect(path)
    connection.execute(VALUATION_DDL)
    connection.execute(
        "INSERT INTO stock_valuation VALUES (?,?,?,?,?,?,?,?)",
        ("920000", "2026-08-01", pe, 1.0, 2.0, 100.0, 10.0, 0.5),
    )
    connection.commit()
    connection.close()


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def enabled_config(shadow: Path, evidence: Path | None = None, policy: str = "open") -> DictConfig:
    values = {
        "data.valuation_dual_read.enabled": True,
        "data.valuation_dual_read.shadow_db": str(shadow),
        "data.valuation_dual_read.shadow_sha256": sha(shadow),
        "data.valuation_dual_read.mode": "full",
        "data.valuation_dual_read.failure_policy": policy,
        "data.valuation_dual_read.probe_codes": "920000",
        "data.valuation_dual_read.probe_kinds": "latest,pit",
        "data.valuation_dual_read.probe_as_of_date": "2026-08-01",
    }
    if evidence is not None:
        values["data.valuation_dual_read.evidence_dir"] = str(evidence)
    return DictConfig(values)


def test_default_off_creates_no_service_and_touches_no_paths(tmp_path):
    assert service_from_config(tmp_path / "missing-formal.db", DictConfig({})) is None
    assert list(tmp_path.iterdir()) == []


def test_service_start_preflights_runs_probes_and_is_idempotent(tmp_path, monkeypatch):
    formal, shadow = tmp_path / "formal.db", tmp_path / "shadow.db"
    make_db(formal)
    make_db(shadow)
    service = service_from_config(formal, enabled_config(shadow))
    assert service is not None
    calls = 0
    original = service.observer.preflight

    def counted_preflight():
        nonlocal calls
        calls += 1
        return original()

    monkeypatch.setattr(service.observer, "preflight", counted_preflight)
    first = service.start()
    second = service.start()

    assert calls == 1
    assert first == second
    assert first["preflight"]["status"] == "PASS_DUAL_READ_PREFLIGHT"
    assert first["summary"]["status_counts"] == {"MATCH": 2}
    assert service.health().status == "green"


def test_fail_open_expected_mismatch_is_yellow_and_reportable(tmp_path):
    formal, shadow = tmp_path / "formal.db", tmp_path / "shadow.db"
    make_db(formal, pe=10.0)
    make_db(shadow, pe=20.0)
    service = service_from_config(formal, enabled_config(shadow, policy="open"))

    metadata = service.start()
    health = service.health()

    assert metadata["error"] is None
    assert metadata["summary"]["blocking"] == 2
    assert metadata["summary"]["mismatch_class_counts"] == {"value_mismatch": 2}
    assert health.status == "yellow"
    assert health.checks[-1].status == "warn"


def test_fail_closed_mismatch_blocks_start_after_evidence(tmp_path):
    formal, shadow = tmp_path / "formal.db", tmp_path / "shadow.db"
    evidence = tmp_path / "evidence"
    make_db(formal, pe=10.0)
    make_db(shadow, pe=20.0)
    service = service_from_config(formal, enabled_config(shadow, evidence, policy="closed"))

    with pytest.raises(DualReadMismatchError, match="value_mismatch"):
        service.start()

    metadata = service.metadata()
    assert metadata["error"]["error_type"] == "DualReadMismatchError"
    assert service.health().status == "red"
    payloads = [json.loads(path.read_text()) for path in evidence.glob("*.json")]
    assert any(payload.get("event") == "startup-failure" for payload in payloads)
    assert not list(evidence.glob("*.tmp"))


def test_preflight_timeout_is_red_when_open_and_blocks_when_closed(tmp_path):
    formal, shadow = tmp_path / "formal.db", tmp_path / "shadow.db"
    make_db(formal)
    make_db(shadow)

    open_config = enabled_config(shadow, policy="open")
    open_config.values["data.valuation_dual_read.preflight_timeout_seconds"] = 1e-12
    open_service = service_from_config(formal, open_config)
    metadata = open_service.start()
    assert metadata["error"]["error_type"] == "DualReadTimeoutError"
    assert open_service.health().status == "red"

    closed_config = enabled_config(shadow, policy="closed")
    closed_config.values["data.valuation_dual_read.preflight_timeout_seconds"] = 1e-12
    closed_service = service_from_config(formal, closed_config)
    with pytest.raises(DualReadTimeoutError, match="preflight exceeded"):
        closed_service.start()


def test_fail_open_bad_preflight_is_red_but_does_not_raise(tmp_path):
    formal, shadow = tmp_path / "formal.db", tmp_path / "shadow.db"
    make_db(formal)
    make_db(shadow)
    config = enabled_config(shadow, policy="open")
    config.values["data.valuation_dual_read.shadow_sha256"] = "0" * 64
    service = service_from_config(formal, config)

    metadata = service.start()

    assert metadata["error"]["error_type"] == "DualReadSafetyError"
    assert service.health().status == "red"
    with pytest.raises(DualReadMismatchError, match="not ready"):
        service.compare(ValuationQuery("latest", "920000", as_of_date="2026-08-01"))


def test_invalid_probe_configuration_fails_before_database_access(tmp_path):
    formal, shadow = tmp_path / "formal.db", tmp_path / "shadow.db"
    make_db(shadow)
    config = enabled_config(shadow)
    config.values["data.valuation_dual_read.probe_kinds"] = "range"
    with pytest.raises(ValueError, match="probe_start_date"):
        service_from_config(formal, config)
    assert not formal.exists()


def test_compare_after_start_updates_metadata_without_mutating_inputs(tmp_path):
    formal, shadow = tmp_path / "formal.db", tmp_path / "shadow.db"
    make_db(formal)
    make_db(shadow)
    before = {formal: sha(formal), shadow: sha(shadow)}
    service = service_from_config(formal, enabled_config(shadow))
    service.start()

    result = service.compare(ValuationQuery("latest", "920000", as_of_date="2026-08-01"))

    assert result.status == "MATCH"
    assert service.metadata()["summary"]["total"] == 3
    assert {path: sha(path) for path in before} == before
    assert not Path(str(formal) + "-wal").exists()
    assert not Path(str(shadow) + "-wal").exists()
