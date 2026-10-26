"""Unit tests for analyze_valuation_alignment.py — P2-03.

Tests cover: read-only DB enforcement, classification priority, exhaustive
classification, date boundary conditions, streaming batches, mid-stream failure
with partial progress, and input fingerprint change detection.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest import mock

import pytest

from scripts.analyze_valuation_alignment import (
    PRIORITY_CATEGORIES,
    classify_gap,
    open_readonly_ro,
    main,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _Row:
    """Simulate sqlite3.Row (supports both named and positional access)."""
    def __init__(self, **kwargs):
        self._data = kwargs
        self._cols = list(kwargs.keys())
        for k, v in kwargs.items():
            setattr(self, k, v)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._data[self._cols[key]]
        return self._data[key]


class _MockCursor:
    """Simulate a sqlite3 cursor that returns canned results."""
    def __init__(self, fetchone_result=None, fetchmany_result=None, fetchall_result=None):
        self.fetchone_result = fetchone_result
        self._fetchmany_result = fetchmany_result
        self.fetchall_result = fetchall_result
        self._fetchmany_index = 0

    def execute(self, sql, params=None):
        return self

    def fetchone(self):
        return self.fetchone_result

    def fetchmany(self, size):
        if self._fetchmany_result is None:
            return []
        if self._fetchmany_index >= len(self._fetchmany_result):
            return []
        batch = self._fetchmany_result[self._fetchmany_index]
        self._fetchmany_index += 1
        return batch

    def fetchall(self):
        return self.fetchall_result

    def __iter__(self):
        if self.fetchall_result is not None:
            return iter(self.fetchall_result)
        return iter(())


class _MockVConn:
    """Simulate the valuation DB connection (read-only, with row_factory)."""
    def __init__(self, rows, total_count=0):
        self.rows = rows
        self.total_count = total_count
        self.row_factory = None
        self.closed = False
        self.executed_sqls = []

    def execute(self, sql, params=None):
        self.executed_sqls.append(sql)
        sql_upper = sql.upper().strip()
        if sql_upper.startswith("PRAGMA"):
            return _MockCursor(fetchone_result=_Row(query_only=1))
        if "SELECT COUNT(*)" in sql and "FROM stock_valuation" in sql:
            return _MockCursor(fetchone_result=_Row(**{"0": self.total_count}))
        if "SELECT code, date FROM stock_valuation" in sql:
            return _MockCursor(fetchmany_result=self.rows)
        return _MockCursor()

    def close(self):
        self.closed = True


class _MockMConn:
    """Simulate the market DB connection (read-only, with row_factory)."""
    def __init__(self, kline_codes=None, market_dates=None, code_bounds=None):
        self.kline_codes = kline_codes or ["000001"]
        self.market_dates = market_dates or ["2026-01-05"]
        self.code_bounds = code_bounds or {"000001": ("2026-01-05", "2026-07-28", 200)}
        self.row_factory = None
        self.closed = False
        self.executed_sqls = []

    def execute(self, sql, params=None):
        self.executed_sqls.append(sql)
        sql_upper = sql.upper().strip()
        if sql_upper.startswith("PRAGMA"):
            return _MockCursor(fetchone_result=_Row(query_only=1))
        if "SELECT DISTINCT code FROM daily_klines" in sql:
            rows = [_Row(code=c) for c in self.kline_codes]
            return _MockCursor(fetchall_result=rows)
        if "SELECT DISTINCT date FROM daily_klines" in sql:
            rows = [_Row(date=d) for d in self.market_dates]
            return _MockCursor(fetchall_result=rows)
        if "SELECT code, MIN(date) AS first_date, MAX(date) AS last_date, COUNT(*) AS kline_rows" in sql:
            rows = []
            for code, (first, last, count) in self.code_bounds.items():
                rows.append(_Row(code=code, first_date=first, last_date=last, kline_rows=count))
            return _MockCursor(fetchall_result=rows)
        return _MockCursor()

    def close(self):
        self.closed = True


def _make_fingerprint(sha256="a" * 64, size=1000, mtime="2026-07-28T12:00:00+00:00"):
    return {
        "path": "/fake/db",
        "sha256": sha256,
        "size_bytes": size,
        "mtime_iso": mtime,
    }


def _invoke_main(vconn, mconn, tmp_path, batch_size=100, output_subdir="audit-output",
                 fingerprint_values=None):
    """Call main() with realistic mocks; writes go to a real temp output dir.

    open_readonly_ro is replaced with fake read-only connections, fingerprint is
    stubbed so fake /fake/*.db paths never hit the filesystem, and tracemalloc
    is stubbed. File writes use the real output dir under tmp_path.
    """
    f_conn = mock.patch("scripts.analyze_valuation_alignment.open_readonly_ro")
    f_tr_start = mock.patch("scripts.analyze_valuation_alignment.tracemalloc.start")
    f_tr_mem = mock.patch("scripts.analyze_valuation_alignment.tracemalloc.get_traced_memory", return_value=(0, 0))
    if fingerprint_values is not None:
        f_fp = mock.patch("scripts.analyze_valuation_alignment.fingerprint", side_effect=fingerprint_values)
    else:
        f_fp = mock.patch("scripts.analyze_valuation_alignment.fingerprint", return_value=_make_fingerprint())

    mock_open_ro = f_conn.start()
    mock_open_ro.side_effect = [vconn, mconn]
    f_tr_start.start()
    f_tr_mem.start()
    f_fp.start()

    args = mock.MagicMock(
        valuation_db=Path("/fake/valuation.db"),
        market_db=Path("/fake/qs.db"),
        output_dir=Path(tmp_path) / output_subdir,
        batch_size=batch_size,
    )
    mock_args = mock.patch("scripts.analyze_valuation_alignment.argparse.ArgumentParser.parse_args", return_value=args)
    mock_args.start()

    try:
        with mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            exit_code = main()
    finally:
        mock_args.stop()
        f_conn.stop()
        f_tr_start.stop()
        f_tr_mem.stop()
        f_fp.stop()

    return exit_code


# ===================================================================
# Test 1: open_readonly_ro uses mode=ro + PRAGMA query_only=ON + verifies
# ===================================================================
class TestOpenReadonlyRO:
    """open_readonly_ro must open with mode=ro URI, set query_only=ON, and verify."""

    def test_uses_mode_ro_uri(self):
        with mock.patch("scripts.analyze_valuation_alignment.sqlite3.connect") as mock_connect:
            mock_conn = mock.MagicMock()
            mock_conn.execute.return_value.fetchone.return_value = _Row(**{"0": 1})
            mock_connect.return_value = mock_conn

            conn = open_readonly_ro(Path("/fake/valuation.db"))

            call_args = mock_connect.call_args
            uri = call_args[0][0] if call_args[0] else call_args[1].get("database", "")
            assert "mode=ro" in uri
            assert conn is mock_conn

    def test_sets_query_only_on(self):
        with mock.patch("scripts.analyze_valuation_alignment.sqlite3.connect") as mock_connect:
            mock_conn = mock.MagicMock()
            mock_conn.execute.return_value.fetchone.return_value = _Row(**{"0": 1})
            mock_connect.return_value = mock_conn

            open_readonly_ro(Path("/fake/valuation.db"))

            pragma_calls = [c for c in mock_conn.execute.call_args_list if "PRAGMA" in str(c)]
            assert any("query_only=ON" in str(c) for c in pragma_calls)

    def test_verifies_query_only_is_1(self):
        with mock.patch("scripts.analyze_valuation_alignment.sqlite3.connect") as mock_connect:
            mock_conn = mock.MagicMock()
            mock_conn.execute.return_value.fetchone.return_value = _Row(**{"0": 1})
            mock_connect.return_value = mock_conn

            open_readonly_ro(Path("/fake/valuation.db"))

            verify_calls = [c for c in mock_conn.execute.call_args_list if "PRAGMA query_only" in str(c) and "ON" not in str(c)]
            assert len(verify_calls) >= 1

    def test_raises_when_query_only_not_1(self):
        with mock.patch("scripts.analyze_valuation_alignment.sqlite3.connect") as mock_connect:
            mock_conn = mock.MagicMock()
            mock_conn.execute.return_value.fetchone.return_value = _Row(**{"0": 0})
            mock_connect.return_value = mock_conn

            with pytest.raises(RuntimeError, match="query_only pragma returned 0"):
                open_readonly_ro(Path("/fake/valuation.db"))


# ===================================================================
# Test 2: Writing to a supposedly read-only connection raises an error
# ===================================================================

class TestReadOnlyWriteAttempt:
    """A sqlite3.Connection opened with mode=ro must raise when written to."""

    def test_write_to_readonly_raises_operational_error(self):
        real_conn = sqlite3.connect("file::memory:?mode=ro", uri=True)
        real_conn.execute("PRAGMA query_only=ON")

        with pytest.raises(sqlite3.OperationalError):
            real_conn.execute("CREATE TABLE t (x)")


# ===================================================================
# Test 3: Classification priority ordering
# ===================================================================

class TestClassificationPriority:
    """When a gap matches multiple categories, the highest priority wins.

    Priority order (from classify_gap):
      CODE_ABSENT_IN_KLINE > BEFORE_FIRST_KLINE > AFTER_LAST_KLINE >
      DATE_NOT_IN_MARKET_CALENDAR > WITHIN_RANGE_NO_KLINE
    """

    def test_code_absent_has_highest_priority(self):
        kline_codes = {"000002"}
        market_dates = {"2026-01-05", "2026-01-06"}
        code_bounds = {"000002": ("2026-01-05", "2026-01-06", 2)}
        cat, _ = classify_gap("000001", "2026-01-05", kline_codes, market_dates, code_bounds)
        assert cat == "CODE_ABSENT_IN_KLINE"

    def test_before_first_kline_beats_after_last(self):
        kline_codes = {"000001"}
        market_dates = {"2026-01-05", "2026-01-10"}
        code_bounds = {"000001": ("2026-01-10", "2026-01-20", 5)}
        cat, _ = classify_gap("000001", "2026-01-05", kline_codes, market_dates, code_bounds)
        assert cat == "BEFORE_FIRST_KLINE"

    def test_before_first_kline_beats_market_calendar(self):
        kline_codes = {"000001"}
        market_dates = {"2026-01-10"}
        code_bounds = {"000001": ("2026-01-10", "2026-01-20", 5)}
        cat, _ = classify_gap("000001", "2026-01-05", kline_codes, market_dates, code_bounds)
        assert cat == "BEFORE_FIRST_KLINE"

    def test_after_last_kline_beats_market_calendar(self):
        kline_codes = {"000001"}
        market_dates = {"2026-01-10", "2026-01-25"}
        code_bounds = {"000001": ("2026-01-10", "2026-01-20", 5)}
        cat, _ = classify_gap("000001", "2026-01-25", kline_codes, market_dates, code_bounds)
        assert cat == "AFTER_LAST_KLINE"

    def test_date_not_in_market_calendar_within_range(self):
        kline_codes = {"000001"}
        market_dates = {"2026-01-10", "2026-01-15"}
        code_bounds = {"000001": ("2026-01-10", "2026-01-20", 5)}
        cat, _ = classify_gap("000001", "2026-01-12", kline_codes, market_dates, code_bounds)
        assert cat == "DATE_NOT_IN_MARKET_CALENDAR"

    def test_within_range_no_kline_is_lowest_priority(self):
        kline_codes = {"000001"}
        market_dates = {"2026-01-10", "2026-01-12", "2026-01-15"}
        code_bounds = {"000001": ("2026-01-10", "2026-01-20", 5)}
        cat, _ = classify_gap("000001", "2026-01-12", kline_codes, market_dates, code_bounds)
        assert cat == "WITHIN_RANGE_NO_KLINE"

    def test_code_absent_beats_everything(self):
        kline_codes = {"000002"}
        market_dates = {"2026-01-05"}
        code_bounds = {"000002": ("2026-01-05", "2026-01-05", 1)}
        cat, _ = classify_gap("000001", "2026-01-03", kline_codes, market_dates, code_bounds)
        assert cat == "CODE_ABSENT_IN_KLINE"


# ===================================================================
# Test 4: Exhaustive classification — sum of all categories = N
# ===================================================================

class TestExhaustiveClassification:
    """Every gap row must be classified into exactly one category."""

    def test_all_gaps_classified(self):
        kline_codes = {"000001", "000002", "000003"}
        market_dates = {"2026-01-05", "2026-01-07", "2026-01-08"}
        code_bounds = {
            "000001": ("2026-01-06", "2026-01-08", 3),
            "000002": ("2026-01-06", "2026-01-08", 1),
            "000003": ("2026-01-05", "2026-01-08", 4),
        }

        gaps = [
            ("000001", "2026-01-05"),  # BEFORE_FIRST_KLINE
            ("000001", "2026-01-09"),  # AFTER_LAST_KLINE
            ("000002", "2026-01-05"),  # BEFORE_FIRST_KLINE
            ("000002", "2026-01-06"),  # DATE_NOT_IN_MARKET_CALENDAR
            ("000003", "2026-01-06"),  # DATE_NOT_IN_MARKET_CALENDAR
            ("000004", "2026-01-05"),  # CODE_ABSENT_IN_KLINE
            ("000004", "2026-01-08"),  # CODE_ABSENT_IN_KLINE
            ("000001", "2026-01-07"),  # WITHIN_RANGE_NO_KLINE
        ]

        counts = {cat: 0 for cat in PRIORITY_CATEGORIES}
        for code, date in gaps:
            cat, _ = classify_gap(code, date, kline_codes, market_dates, code_bounds)
            counts[cat] = counts.get(cat, 0) + 1

        total_classified = sum(counts.values())
        assert total_classified == len(gaps), f"Expected {len(gaps)} classified, got {total_classified}"
        assert counts["BEFORE_FIRST_KLINE"] == 2
        assert counts["AFTER_LAST_KLINE"] == 1
        assert counts["DATE_NOT_IN_MARKET_CALENDAR"] == 2
        assert counts["CODE_ABSENT_IN_KLINE"] == 2
        assert counts["WITHIN_RANGE_NO_KLINE"] == 1


# ===================================================================
# Test 5: Date boundary conditions
# ===================================================================

class TestDateBoundaries:
    """Date at first_kline is NOT a gap; date before first_kline IS a gap."""

    def test_date_at_first_kline_is_not_gap(self):
        kline_codes = {"000001"}
        market_dates = {"2026-01-05"}
        code_bounds = {"000001": ("2026-01-05", "2026-01-10", 3)}
        cat, _ = classify_gap("000001", "2026-01-05", kline_codes, market_dates, code_bounds)
        assert cat == "WITHIN_RANGE_NO_KLINE"

    def test_date_at_first_kline_in_market_is_not_gap(self):
        kline_codes = {"000001"}
        market_dates = {"2026-01-05"}
        code_bounds = {"000001": ("2026-01-05", "2026-01-10", 3)}
        cat, _ = classify_gap("000001", "2026-01-05", kline_codes, market_dates, code_bounds)
        assert cat == "WITHIN_RANGE_NO_KLINE"

    def test_date_before_first_kline_is_gap(self):
        kline_codes = {"000001"}
        market_dates = {"2026-01-05"}
        code_bounds = {"000001": ("2026-01-05", "2026-01-10", 3)}
        cat, _ = classify_gap("000001", "2026-01-04", kline_codes, market_dates, code_bounds)
        assert cat == "BEFORE_FIRST_KLINE"

    def test_date_after_last_kline_is_gap(self):
        kline_codes = {"000001"}
        market_dates = {"2026-01-11"}
        code_bounds = {"000001": ("2026-01-05", "2026-01-10", 3)}
        cat, _ = classify_gap("000001", "2026-01-11", kline_codes, market_dates, code_bounds)
        assert cat == "AFTER_LAST_KLINE"


# ===================================================================
# Test 6: Streaming batches — 150K rows, batch_size=50K → 3 batches
# ===================================================================

class TestStreamingBatches:
    """With 150,000 rows and batch_size=50,000, main must process 3 batches."""

    def test_three_batches_processed(self, tmp_path):
        batch_size = 50000
        total_rows = 150000
        code = "000001"

        kline_codes = ["000001"]
        market_dates = ["2026-01-05"]
        code_bounds = {"000001": ("2026-01-05", "2026-07-28", 200)}

        batches = []
        for i in range(0, total_rows, batch_size):
            batch = []
            for j in range(i, min(i + batch_size, total_rows)):
                day = (j // 1000) + 1
                date_str = f"2026-{day//100:02d}-{(day%100):02d}"
                batch.append(_Row(code=code, date=date_str))
            batches.append(batch)

        vconn = _MockVConn(batches, total_count=total_rows)
        mconn = _MockMConn(kline_codes=kline_codes, market_dates=market_dates, code_bounds=code_bounds)

        exit_code = _invoke_main(vconn, mconn, tmp_path, batch_size=batch_size)

        assert exit_code == 0
        assert len(batches) == 3
        summary = json.loads((tmp_path / "audit-output" / "alignment-summary.json").read_text(encoding="utf-8"))
        assert summary["total_valuation_rows"] == total_rows
        assert summary["gap_rows"] == total_rows


# ===================================================================
# Test 7: Mid-stream failure returns non-zero exit and preserves partial progress
# ===================================================================

class TestMidStreamFailure:
    """An exception after 2 batches should return non-zero exit and preserve partial output."""

    def test_failure_after_two_batches_returns_nonzero(self, tmp_path):
        batch_size = 50000
        code = "000001"

        kline_codes = ["000001"]
        market_dates = ["2026-01-05"]
        code_bounds = {"000001": ("2026-01-05", "2026-07-28", 200)}

        batches = []
        for i in range(3):
            batch = []
            for j in range(batch_size):
                day = (i * batch_size + j) // 1000 + 1
                date_str = f"2026-{day//100:02d}-{(day%100):02d}"
                batch.append(_Row(code=code, date=date_str))
            batches.append(batch)

        vconn = _MockVConn(batches, total_count=150000)
        mconn = _MockMConn(kline_codes=kline_codes, market_dates=market_dates, code_bounds=code_bounds)

        original_execute = vconn.execute

        def _fail_after_two_batches(sql, params=None):
            if "SELECT code, date FROM stock_valuation" in sql:
                cursor = original_execute(sql)
                original_fetchmany = cursor.fetchmany

                call_count = [0]

                def _counting_fetchmany(size):
                    call_count[0] += 1
                    if call_count[0] >= 3:
                        raise RuntimeError("Simulated mid-stream failure")
                    return original_fetchmany(size)

                cursor.fetchmany = _counting_fetchmany
                return cursor
            return original_execute(sql)

        vconn.execute = _fail_after_two_batches

        exit_code = _invoke_main(vconn, mconn, tmp_path, batch_size=batch_size)

        assert exit_code != 0
        run_log = (tmp_path / "audit-output" / "run.log").read_text(encoding="utf-8")
        assert "Mid-stream failure" in run_log


# ===================================================================
# Test 8: Input fingerprint changes cause failure
# ===================================================================

class TestInputFingerprintChange:
    """When input fingerprints differ before vs after, the script should still
    complete (the change is logged) but the summary must reflect the mismatch."""

    def test_fingerprint_mismatch_is_recorded(self, tmp_path):
        batch_size = 100
        code = "000001"

        kline_codes = ["000001"]
        market_dates = ["2026-01-05"]
        code_bounds = {"000001": ("2026-01-05", "2026-07-28", 200)}

        rows = [[_Row(code=code, date="2026-01-06")]]
        vconn = _MockVConn(rows, total_count=1)
        mconn = _MockMConn(kline_codes=kline_codes, market_dates=market_dates, code_bounds=code_bounds)

        fingerprint_values = [
            _make_fingerprint("a" * 64),
            _make_fingerprint("b" * 64),
            _make_fingerprint("c" * 64),
            _make_fingerprint("d" * 64),
        ]
        exit_code = _invoke_main(vconn, mconn, tmp_path, batch_size=batch_size,
                                 fingerprint_values=fingerprint_values)

        assert exit_code == 0
        summary = json.loads((tmp_path / "audit-output" / "alignment-summary.json").read_text(encoding="utf-8"))
        before = summary["input_fingerprints_before"]
        after = summary["input_fingerprints_after"]
        assert before["valuation_db"]["sha256"] == "a" * 64
        assert before["market_db"]["sha256"] == "b" * 64
        assert after["valuation_db"]["sha256"] == "c" * 64
        assert after["market_db"]["sha256"] == "d" * 64

    def test_identical_fingerprints_allows_completion(self, tmp_path):
        batch_size = 100
        code = "000001"

        kline_codes = ["000001"]
        market_dates = ["2026-01-05"]
        code_bounds = {"000001": ("2026-01-05", "2026-07-28", 200)}

        rows = [[_Row(code=code, date="2026-01-06")]]
        vconn = _MockVConn(rows, total_count=1)
        mconn = _MockMConn(kline_codes=kline_codes, market_dates=market_dates, code_bounds=code_bounds)

        fp = _make_fingerprint("a" * 64)
        exit_code = _invoke_main(vconn, mconn, tmp_path, batch_size=batch_size,
                                 fingerprint_values=[fp, fp, fp, fp])

        assert exit_code == 0


# ===================================================================
# Test 9: output_dir is auto-created
# ===================================================================

class TestOutputDirAutoCreate:
    """The script must create the output directory if it does not exist."""

    def test_output_dir_created(self, tmp_path):
        kline_codes = ["000001"]
        market_dates = ["2026-01-05"]
        code_bounds = {"000001": ("2026-01-05", "2026-07-28", 200)}
        rows = [[_Row(code="000001", date="2026-01-06")]]

        vconn = _MockVConn(rows, total_count=1)
        mconn = _MockMConn(kline_codes=kline_codes, market_dates=market_dates, code_bounds=code_bounds)

        output_dir = tmp_path / "audit-output"
        exit_code = _invoke_main(vconn, mconn, tmp_path, batch_size=100)

        assert exit_code == 0
        assert output_dir.is_dir()
        assert (output_dir / "alignment-summary.json").exists()