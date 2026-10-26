"""Unit tests for classify_valuation_empty_results.py — P2-04, no network, no real DBs."""

import json
import sqlite3
from unittest import mock

import pytest

from scripts.classify_valuation_empty_results import (
    CATEGORIES,
    RETRY_ALLOWED,
    classify_code,
    open_readonly_ro,
    main,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_info(**overrides):
    defaults = {
        "code": "000001",
        "name": "平安银行",
        "industry": "银行",
        "market": "SZ",
        "list_date": "20000101",
        "is_active": 1,
    }
    defaults.update(overrides)
    return defaults


def _make_kline_info(code="000001", count=100, first="20200101", last="20260701"):
    return {"code": code, "count": count, "first_date": first, "last_date": last}


def _make_provenance_info(code="000001", status="failed", error_type="BackfillRefusedError",
                          error_message="", rows_written=0, elapsed_s=0.5):
    return {
        "code": code,
        "status": status,
        "error_type": error_type,
        "error_message": error_message,
        "rows_written": rows_written,
        "elapsed_s": elapsed_s,
    }


def _make_backfill_record(code, event="failed", **overrides):
    rec = {"event": event, "code": code}
    rec.update(overrides)
    return rec


def _make_stock_info_row(code, **overrides):
    defaults = {
        "code": code,
        "name": f"Company{code}",
        "industry": "Test",
        "market": "SZ",
        "list_date": "20000101",
        "is_active": 1,
    }
    defaults.update(overrides)
    return defaults


def _make_kline_row(code, count=100, first="20200101", last="20260701"):
    return {"code": code, "count": count, "first_date": first, "last_date": last}


def _make_provenance_row(code, status="failed", error_type="BackfillRefusedError",
                         error_message="", rows_written=0, elapsed_s=0.5):
    return {
        "code": code,
        "status": status,
        "error_type": error_type,
        "error_message": error_message,
        "rows_written": rows_written,
        "elapsed_s": elapsed_s,
    }


class _MockCursor:
    def __init__(self, rows=None, fetchone_result=None):
        self.rows = rows or []
        self._fetchone_result = fetchone_result

    def execute(self, sql, params=None):
        return self

    def __iter__(self):
        return iter(self.rows)

    def fetchone(self):
        if self._fetchone_result is not None:
            return self._fetchone_result
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class _MockConnection:
    def __init__(self, stock_info_rows=None, kline_rows=None, provenance_rows=None,
                 query_only_value=1):
        self.stock_info_rows = stock_info_rows or []
        self.kline_rows = kline_rows or []
        self.provenance_rows = provenance_rows or []
        self._query_only_value = query_only_value
        self.row_factory = None
        self.closed = False
        self.executed_pragmas = []

    def execute(self, sql, params=None):
        sql_stripped = sql.strip()
        sql_upper = sql_stripped.upper()
        if sql_upper.startswith("PRAGMA"):
            self.executed_pragmas.append(sql_stripped)
            if "QUERY_ONLY" in sql_upper and "=" not in sql_upper:
                return _MockCursor(fetchone_result=(self._query_only_value,))
            if "QUERY_ONLY=ON" in sql_upper:
                return _MockCursor()
            if "TEMP_STORE=MEMORY" in sql_upper:
                return _MockCursor()
            if "CACHE_SIZE" in sql_upper:
                return _MockCursor()
            return _MockCursor()
        if "FROM STOCK_INFO" in sql_upper:
            return _MockCursor(rows=self.stock_info_rows)
        if "FROM DAILY_KLINES" in sql_upper:
            return _MockCursor(rows=self.kline_rows)
        if "FROM VALUATION_BACKFILL_STATE" in sql_upper:
            return _MockCursor(rows=self.provenance_rows)
        return _MockCursor()

    def close(self):
        self.closed = True


def _build_backfill_jsonl_lines(records):
    return [json.dumps(r) + "\n" for r in records]


def _build_alignment_csv(rows):
    if not rows:
        return ""
    header = list(rows[0].keys())
    lines = [",".join(header)]
    for row in rows:
        lines.append(",".join(str(row.get(k, "")) for k in header))
    return "\n".join(lines)


def _make_args(tmp_path, backfill_records, alignment_rows=None):
    """Create realistic CLI args backed by real files in tmp_path.

    The real argparse parser declares ``type=Path``, so the mocked args must
    carry real ``pathlib.Path`` values — not strings.
    """
    out_dir = tmp_path / "out"
    backfill_path = tmp_path / "backfill.jsonl"
    backfill_path.write_text("".join(_build_backfill_jsonl_lines(backfill_records)),
                             encoding="utf-8")
    alignment_path = tmp_path / "alignment.csv"
    alignment_path.write_text(_build_alignment_csv(alignment_rows or []), encoding="utf-8")
    return out_dir, mock.MagicMock(
        market_db=tmp_path / "market.db",
        valuation_db=tmp_path / "valuation.db",
        backfill_jsonl=backfill_path,
        output_dir=out_dir,
        alignment_by_code=alignment_path,
        drill_json=None,
    )


def _run_main(tmp_path, conn, backfill_records, alignment_rows=None):
    """Run main() against real input/output files, mocking only sqlite3.connect."""
    out_dir, args = _make_args(tmp_path, backfill_records, alignment_rows)
    with (
        mock.patch("scripts.classify_valuation_empty_results.sqlite3.connect",
                   return_value=conn),
        mock.patch("scripts.classify_valuation_empty_results.argparse.ArgumentParser.parse_args",
                   return_value=args),
    ):
        ret = main()
    return ret, out_dir


def _read_per_code(out_dir):
    return json.loads((out_dir / "classification-per-code.json").read_text(encoding="utf-8"))


# ===================================================================
# Test 1: Exactly 75 unique codes from backfill JSONL
# ===================================================================

class Test75Codes:
    """Verify exactly 75 unique codes are processed."""

    def test_75_unique_codes(self, tmp_path):
        codes = [f"{100000 + i:06d}" for i in range(75)]
        backfill_records = [_make_backfill_record(c) for c in codes]

        conn = _MockConnection(
            stock_info_rows=[_make_stock_info_row(c) for c in codes],
            kline_rows=[_make_kline_row(c) for c in codes],
            provenance_rows=[_make_provenance_row(c) for c in codes],
        )

        ret, out_dir = _run_main(tmp_path, conn, backfill_records)

        assert ret == 0
        per_code = _read_per_code(out_dir)
        assert per_code is not None
        assert len(per_code) == 75
        codes_in_output = {r["code"] for r in per_code}
        assert len(codes_in_output) == 75
        assert codes_in_output == set(codes)


# ===================================================================
# Test 2: Non-empty-result codes are excluded
# ===================================================================

class TestNonEmptyExcluded:
    """Codes with status=success must NOT be included."""

    def test_success_codes_excluded(self, tmp_path):
        failed_codes = [f"{100000 + i:06d}" for i in range(5)]
        success_codes = [f"{200000 + i:06d}" for i in range(3)]
        backfill_records = (
            [_make_backfill_record(c) for c in failed_codes]
            + [_make_backfill_record(c, event="success", rows_written=100) for c in success_codes]
        )
        all_codes = failed_codes + success_codes

        conn = _MockConnection(
            stock_info_rows=[_make_stock_info_row(c) for c in all_codes],
            kline_rows=[_make_kline_row(c) for c in all_codes],
            provenance_rows=[_make_provenance_row(c) for c in all_codes],
        )

        ret, out_dir = _run_main(tmp_path, conn, backfill_records)

        assert ret == 0
        per_code = _read_per_code(out_dir)
        assert per_code is not None
        output_codes = {r["code"] for r in per_code}
        assert len(output_codes) == 5
        for c in failed_codes:
            assert c in output_codes, f"Failed code {c} should be included"
        for c in success_codes:
            assert c not in output_codes, f"Success code {c} should NOT be included"


# ===================================================================
# Test 3: Only TRANSIENT_NETWORK allows retry=True
# ===================================================================

class TestRetryAllowed:
    """Only TRANSIENT_NETWORK category may have retry=True."""

    def test_only_transient_network_retry_true(self):
        for cat in CATEGORIES:
            if cat == "TRANSIENT_NETWORK":
                assert cat in RETRY_ALLOWED
            else:
                assert cat not in RETRY_ALLOWED, f"{cat} should not be in RETRY_ALLOWED"

    def test_classify_code_retry_flag(self):
        test_cases = [
            ("000406", _make_info(code="000406"), None, "MERGER_OR_REPLACED"),
            ("000001", _make_info(code="000001", name="*ST平安"), None, "DELISTED_OR_LEGACY"),
            ("001235", _make_info(code="001235"), None, "TEST_OR_POLLUTION"),
            ("999999", _make_info(code="999999", is_active=0), None, "DELISTED_OR_LEGACY"),
            ("000001", _make_info(code="000001", market="SH"),
             _make_kline_info(count=0), "MARKET_LABEL_ERROR"),
        ]
        for code, info, kline, expected_cat in test_cases:
            cat, reason = classify_code(code, info, kline, None, False, None)
            assert cat == expected_cat, f"Expected {expected_cat} for {code}, got {cat}"
            if cat == "TRANSIENT_NETWORK":
                assert cat in RETRY_ALLOWED
            else:
                assert cat not in RETRY_ALLOWED

    def test_transient_network_retry_true_in_output(self):
        info = _make_info(code="000001", market="SZ")
        info2 = _make_info(code="000002", market="SZ")

        cat1, reason1 = classify_code("000001", info, None, {"code": "000001", "error_type": "TimeoutError"}, False, None)
        cat2, reason2 = classify_code("000002", info2, None, {"code": "000002", "error_type": "ConnectionError"}, False, None)

        assert cat1 == "TRANSIENT_NETWORK"
        assert cat1 in RETRY_ALLOWED

        assert cat2 == "TRANSIENT_NETWORK"
        assert cat2 in RETRY_ALLOWED

    def test_all_other_categories_retry_false(self):
        for cat in CATEGORIES:
            if cat == "TRANSIENT_NETWORK":
                continue
            assert cat not in RETRY_ALLOWED

    def test_retry_flag_integration(self, tmp_path):
        codes = [f"{100000 + i:06d}" for i in range(10)]
        backfill_records = [_make_backfill_record(c) for c in codes]

        stock_info_rows = []
        kline_rows = []
        provenance_rows = []
        for i, c in enumerate(codes):
            stock_info_rows.append(_make_stock_info_row(c, market="SZ"))
            kline_rows.append(_make_kline_row(c, count=0))
            if i == 0:
                provenance_rows.append(_make_provenance_row(c, error_type="TimeoutError"))
            else:
                provenance_rows.append(_make_provenance_row(c, error_type="BackfillRefusedError"))

        conn = _MockConnection(
            stock_info_rows=stock_info_rows,
            kline_rows=kline_rows,
            provenance_rows=provenance_rows,
        )

        ret, out_dir = _run_main(tmp_path, conn, backfill_records)

        assert ret == 0
        per_code = _read_per_code(out_dir)
        assert per_code is not None
        for r in per_code:
            if r["code"] == codes[0]:
                assert r["suggest_retry"] is True, (
                    f"{codes[0]} should have retry=True (TRANSIENT_NETWORK)"
                )
            else:
                assert r["suggest_retry"] is False, (
                    f"{r['code']} should have retry=False, got {r['suggest_retry']}"
                )


# ===================================================================
# Test 4: Low-confidence items flagged for manual review
# ===================================================================

class TestManualReview:
    """UNKNOWN_MANUAL_REVIEW items should be flagged appropriately."""

    def test_unknown_manual_review_category(self):
        code = "999999"
        info = _make_info(code=code, market="SH", name="正常股票")
        kline = _make_kline_info(count=0)
        prov = _make_provenance_info(error_type="")
        cat, reason = classify_code(code, info, kline, prov, False, None)
        assert cat == "UNKNOWN_MANUAL_REVIEW"
        assert "no matching classification rule" in reason.lower()

    def test_manual_review_in_output_fields(self, tmp_path):
        codes = [f"{100000 + i:06d}" for i in range(5)]
        backfill_records = [_make_backfill_record(c) for c in codes]

        stock_info_rows = []
        kline_rows = []
        provenance_rows = []
        for c in codes:
            stock_info_rows.append(_make_stock_info_row(c, market="SH", name="正常股票"))
            kline_rows.append(_make_kline_row(c, count=0))
            provenance_rows.append(_make_provenance_row(c, error_type=""))

        conn = _MockConnection(
            stock_info_rows=stock_info_rows,
            kline_rows=kline_rows,
            provenance_rows=provenance_rows,
        )

        ret, out_dir = _run_main(tmp_path, conn, backfill_records)

        assert ret == 0
        per_code = _read_per_code(out_dir)
        assert per_code is not None
        for r in per_code:
            assert r["primary_category"] == "UNKNOWN_MANUAL_REVIEW"
            assert r["suggest_retry"] is False

    @pytest.mark.parametrize("code,info,expected_cat", [
        ("000001", {"code": "000001", "name": "正常股票", "market": "SH", "list_date": "20000101", "is_active": 1}, "MARKET_LABEL_ERROR"),
        ("999999", {"code": "999999", "name": "未知", "market": "SZ", "list_date": "20000101", "is_active": 1}, "UNKNOWN_MANUAL_REVIEW"),
    ])
    def test_various_unknown_cases(self, code, info, expected_cat):
        kline = _make_kline_info(count=0)
        prov = _make_provenance_info(error_type="")
        cat, reason = classify_code(code, info, kline, prov, False, None)
        assert cat == expected_cat


# ===================================================================
# Test 5: Output fields are complete
# ===================================================================

class TestOutputFields:
    """Every output record must contain all expected fields."""

    EXPECTED_FIELDS = [
        "code",
        "primary_category",
        "suspected_reason",
        "in_pollution_drill",
        "in_p2p3_alignment",
        "suggest_retry",
        "backfill_error_type",
        "backfill_error_message",
        "backfill_elapsed_s",
        "stock_name",
        "stock_market",
        "stock_list_date",
        "stock_is_active",
        "kline_count",
        "kline_first_date",
        "kline_last_date",
    ]

    def test_all_output_fields_present(self, tmp_path):
        codes = [f"{100000 + i:06d}" for i in range(3)]
        backfill_records = [_make_backfill_record(c) for c in codes]

        conn = _MockConnection(
            stock_info_rows=[_make_stock_info_row(c) for c in codes],
            kline_rows=[_make_kline_row(c) for c in codes],
            provenance_rows=[_make_provenance_row(c) for c in codes],
        )

        ret, out_dir = _run_main(tmp_path, conn, backfill_records)

        assert ret == 0
        per_code = _read_per_code(out_dir)
        assert per_code is not None
        for r in per_code:
            for field in self.EXPECTED_FIELDS:
                assert field in r, f"Missing field '{field}' in record for {r.get('code', '?')}"
            assert r["code"] in codes


# ===================================================================
# Test 6: Script refuses to write to source databases (PRAGMA query_only)
# ===================================================================

class TestReadOnlyDatabase:
    """The script must open source databases with PRAGMA query_only=ON verified."""

    def test_open_readonly_ro_sets_query_only(self):
        mock_conn = mock.MagicMock(spec=sqlite3.Connection)
        mock_cursor = mock.MagicMock()
        mock_cursor.fetchone.return_value = (1,)
        mock_conn.execute.return_value = mock_cursor

        with mock.patch("scripts.classify_valuation_empty_results.sqlite3.connect",
                        return_value=mock_conn) as mock_connect:
            result = open_readonly_ro("/fake/path/market.db")

        mock_connect.assert_called_once()
        uri = mock_connect.call_args[0][0]
        assert "mode=ro" in uri, f"Expected read-only URI, got: {uri}"

        pragma_calls = [c[0][0] for c in mock_conn.execute.call_args_list]
        assert "PRAGMA query_only=ON" in pragma_calls
        assert "PRAGMA query_only" in pragma_calls

        assert result is mock_conn

    def test_open_readonly_ro_raises_on_query_only_mismatch(self):
        mock_conn = mock.MagicMock(spec=sqlite3.Connection)
        mock_cursor = mock.MagicMock()
        mock_cursor.fetchone.return_value = (0,)
        mock_conn.execute.return_value = mock_cursor

        with mock.patch("scripts.classify_valuation_empty_results.sqlite3.connect",
                        return_value=mock_conn):
            with pytest.raises(RuntimeError, match="query_only pragma returned 0, expected 1"):
                open_readonly_ro("/fake/path/market.db")

    def test_main_opens_databases_readonly(self, tmp_path):
        codes = [f"{100000 + i:06d}" for i in range(3)]
        backfill_records = [_make_backfill_record(c) for c in codes]

        conn = _MockConnection(
            stock_info_rows=[_make_stock_info_row(c) for c in codes],
            kline_rows=[_make_kline_row(c) for c in codes],
            provenance_rows=[_make_provenance_row(c) for c in codes],
        )

        out_dir, args = _make_args(tmp_path, backfill_records)

        with (
            mock.patch("scripts.classify_valuation_empty_results.sqlite3.connect",
                       return_value=conn) as mock_connect,
            mock.patch("scripts.classify_valuation_empty_results.argparse.ArgumentParser.parse_args",
                       return_value=args),
        ):
            ret = main()

        assert ret == 0
        assert conn.closed, "Database connection should be closed"

        for call_args in mock_connect.call_args_list:
            uri = call_args[0][0] if call_args[0] else ""
            assert "mode=ro" in uri, f"Source DB connect must be read-only: {uri}"

        assert "PRAGMA query_only=ON" in conn.executed_pragmas, (
            "PRAGMA query_only=ON must be executed"
        )
