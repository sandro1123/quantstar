from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path


MODULE_PATH = Path(__file__).parents[3] / "scripts" / "backfill_factors_full.py"
SPEC = importlib.util.spec_from_file_location("backfill_factors_full", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def make_source(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE daily_klines(code TEXT, date TEXT, close REAL, PRIMARY KEY(code,date));
            CREATE TABLE factor_daily(
                date TEXT NOT NULL,
                code TEXT NOT NULL,
                factor_scores TEXT,
                PRIMARY KEY(date,code)
            );
            INSERT INTO daily_klines VALUES ('000001','2026-07-21',10.0);
            INSERT INTO daily_klines VALUES ('000001','2026-07-22',10.5);
            INSERT INTO factor_daily VALUES ('2026-07-21','000001',NULL);
            INSERT INTO factor_daily VALUES ('2026-07-22','000001',NULL);
            """
        )


def processor(_db: Path, date: str) -> list[tuple[str, str]]:
    return [("000001", json.dumps({"value": 1.0}))]


def test_isolated_run_records_checkpoint_and_provenance(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    target = tmp_path / "isolated" / "target.db"
    log = tmp_path / "logs" / "run.jsonl"
    make_source(source)
    before = source.read_bytes()

    result = MODULE.run(source, target, log, processor=processor, repo=tmp_path)

    assert result["status"] == "quality_pass"
    assert result["completed_dates"] == 2
    assert result["rows_written"] == 2
    assert source.read_bytes() == before
    with sqlite3.connect(target) as conn:
        assert conn.execute("SELECT COUNT(*) FROM factor_recompute_checkpoints").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM factor_recompute_provenance").fetchone()[0] == 2
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def test_failure_propagates_to_failed_summary_and_leaves_date_unchanged(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    target = tmp_path / "isolated" / "target.db"
    log = tmp_path / "run.jsonl"
    make_source(source)

    def fail_on_second(_db: Path, date: str) -> list[tuple[str, str]]:
        if date == "2026-07-22":
            raise RuntimeError("deliberate failure")
        return processor(_db, date)

    result = MODULE.run(source, target, log, processor=fail_on_second, repo=tmp_path)

    assert result["status"] == "failed"
    assert result["failures"] == 1
    with sqlite3.connect(target) as conn:
        assert conn.execute("SELECT COUNT(*) FROM factor_recompute_failures").fetchone()[0] == 1
        assert conn.execute(
            "SELECT factor_scores FROM factor_daily WHERE date='2026-07-22'"
        ).fetchone()[0] is None


def test_resume_skips_completed_dates_and_rejects_changed_source(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    target = tmp_path / "isolated" / "target.db"
    log = tmp_path / "run.jsonl"
    make_source(source)
    first = MODULE.run(source, target, log, processor=processor, repo=tmp_path)
    assert first["status"] == "quality_pass"

    second = MODULE.run(source, target, log, processor=processor, repo=tmp_path)
    assert second["already_completed"] == 2
    assert second["completed_dates"] == 0
    assert second["status"] == "quality_pass"

    with sqlite3.connect(source) as conn:
        conn.execute("INSERT INTO daily_klines VALUES ('000002','2026-07-22',9.0)")
        conn.commit()
    try:
        MODULE.run(source, target, log, processor=processor, repo=tmp_path)
    except RuntimeError as exc:
        assert "source database fingerprint changed" in str(exc)
    else:
        raise AssertionError("changed source fingerprint must be rejected")


def test_target_must_be_isolated_and_not_named_qs_db(tmp_path: Path) -> None:
    source = tmp_path / "source" / "qs.db"
    source.parent.mkdir()
    make_source(source)
    for target in (source.parent / "candidate.db", tmp_path / "isolated" / "qs.db"):
        try:
            MODULE.run(source, target, tmp_path / "run.jsonl", processor=processor)
        except ValueError as exc:
            assert "isolated database" in str(exc)
        else:
            raise AssertionError("non-isolated target must be rejected")


def test_quality_gate_rejects_duplicate_or_partial_factor_coverage(tmp_path: Path) -> None:
    for suffix, bad_rows in (
        (
            "duplicate",
            [
                ("000001", json.dumps({"value": 1.0})),
                ("000001", json.dumps({"value": 2.0})),
            ],
        ),
        ("partial", [("000002", json.dumps({"value": 1.0}))]),
    ):
        source = tmp_path / f"source-{suffix}.db"
        target = tmp_path / "isolated" / f"target-{suffix}.db"
        make_source(source)

        result = MODULE.run(
            source,
            target,
            tmp_path / f"{suffix}.jsonl",
            processor=lambda _db, _date, rows=bad_rows: rows,
            repo=tmp_path,
        )

        assert result["status"] == "failed"
        assert result["failures"] == 2
        with sqlite3.connect(target) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM factor_recompute_provenance"
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM factor_recompute_checkpoints"
            ).fetchone()[0] == 0


def test_engine_subset_of_factor_daily_is_accepted(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    make_source(source)
    target = tmp_path / "isolated" / "target.db"

    with sqlite3.connect(source) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO factor_daily VALUES ('2026-07-21','999999',NULL)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO factor_daily VALUES ('2026-07-22','999999',NULL)"
        )
        conn.commit()

    result = MODULE.run(
        source,
        target,
        tmp_path / "subset.jsonl",
        processor=lambda _db, _date: [("000001", json.dumps({"value": 1.0}))],
        repo=tmp_path,
    )

    assert result["status"] == "quality_pass"
    assert result["completed_dates"] == 2
    with sqlite3.connect(target) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM factor_recompute_checkpoints"
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM factor_recompute_provenance"
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT factor_scores FROM factor_daily WHERE date='2026-07-21' AND code='999999'"
        ).fetchone()[0] is None


def test_quality_gate_rejects_nonfinite_scores(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    target = tmp_path / "isolated" / "target.db"
    make_source(source)

    result = MODULE.run(
        source,
        target,
        tmp_path / "nonfinite.jsonl",
        processor=lambda _db, _date: [("000001", '{"value": NaN}')],
        repo=tmp_path,
    )

    assert result["status"] == "failed"
    assert result["failures"] == 2
    with sqlite3.connect(target) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM factor_recompute_provenance"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT factor_scores FROM factor_daily WHERE date='2026-07-21'"
        ).fetchone()[0] is None


def test_source_fingerprint_is_content_based(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    make_source(source)
    first = MODULE.source_fingerprint(source)
    stat = source.stat()

    payload = bytearray(source.read_bytes())
    payload[-1] ^= 1
    source.write_bytes(payload)
    source.touch()
    import os

    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    assert source.stat().st_size == stat.st_size
    assert source.stat().st_mtime_ns == stat.st_mtime_ns
    assert MODULE.source_fingerprint(source) != first


def test_pit_guard_rejects_future_reads(tmp_path: Path) -> None:
    db = tmp_path / "pit.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE audit_read_trace(table_name TEXT, requested_as_of TEXT, max_date_read TEXT)")
        conn.execute("INSERT INTO audit_read_trace VALUES ('stock_financials','2026-07-21','2026-07-22')")
        try:
            MODULE.validate_pit(conn, "2026-07-21")
        except RuntimeError as exc:
            assert "future read" in str(exc)
        else:
            raise AssertionError("future reads must fail the PIT gate")
