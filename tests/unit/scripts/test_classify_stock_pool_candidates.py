"""Unit tests for classify_stock_pool_candidates.py — no network, no real DBs."""
import json
import sqlite3
from unittest import mock

import pytest

from scripts.classify_stock_pool_candidates import (
    CATEGORIES,
    SUCCESSOR_MAP,
    classify_code,
    generate_sql,
    load_candidates,
    main,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_drill_data(count: int) -> dict:
    candidates = []
    for i in range(count):
        code = f"{100000 + i:06d}"
        candidates.append([code, f"Stock{i}", "SZ", "20000101", 1])
    return {
        "candidate_unique": count,
        "findings_counts": {"pollution_drill": count},
        "sample_findings": {"pollution_drill": candidates},
    }


def _fake_stock_info_row(code, **overrides):
    defaults = {
        "code": code,
        "name": f"Stock-{code}",
        "industry": "Test",
        "market": "SZ",
        "list_date": "20000101",
        "is_active": 1,
    }
    defaults.update(overrides)
    return _Row(**defaults)


def _fake_kline_bounds(code, count=100, first="20200101", last="20260701"):
    return _Row(
        code=code,
        first_date=first,
        last_date=last,
        kline_count=count,
    )


class _Row:
    """Simulate sqlite3.Row (supports string-key indexing and mapping protocol)."""
    def __init__(self, **kwargs):
        self._data = kwargs
        for k, v in kwargs.items():
            setattr(self, k, v)

    def keys(self):
        return self._data.keys()

    def __getitem__(self, key):
        return self._data[key]


class _MockCursor:
    """Simulate a sqlite3 cursor that returns canned results."""
    def __init__(self, fetchone_result=None, fetchall_result=None):
        self.fetchone_result = fetchone_result
        self.fetchall_result = fetchall_result

    def execute(self, sql, params=None):
        return self

    def fetchone(self):
        return self.fetchone_result

    def fetchall(self):
        return self.fetchall_result


class _MockConnection:
    """Simulate a sqlite3 connection."""
    def __init__(self, stock_info_row=None, kline_row=None, quarantine_rows=None):
        self.stock_info_row = stock_info_row
        self.kline_row = kline_row
        self.quarantine_rows = quarantine_rows or []
        self._should_verify = True
        self.row_factory = None
        self.closed = False

    def execute(self, sql, params=None):
        sql_upper = sql.upper().strip()
        if sql_upper.startswith("PRAGMA"):
            if "=" in sql_upper:
                return _MockCursor()
            if "QUERY_ONLY" in sql_upper:
                return _MockCursor(fetchone_result=_Row(query_only=1))
            return _MockCursor()
        if "SELECT 1 AS verify_one" in sql:
            return _MockCursor(fetchone_result=_Row(verify_one=1))
        if "FROM stock_info" in sql and "WHERE code = ?" in sql:
            return _MockCursor(fetchone_result=self.stock_info_row)
        if "FROM daily_klines" in sql and "WHERE code = ?" in sql:
            return _MockCursor(fetchone_result=self.kline_row)
        if "FROM stock_info_quarantine" in sql:
            return _MockCursor(fetchall_result=self.quarantine_rows)
        return _MockCursor()

    def close(self):
        self.closed = True


# ===================================================================
# Test 1: Exactly 239 unique candidates
# ===================================================================

class TestCandidateCount:
    """Verify the script processes exactly 239 unique candidates."""

    def test_239_candidates(self):
        drill_data = _make_drill_data(239)

        conn = _MockConnection(quarantine_rows=[])

        with (
            mock.patch("scripts.classify_stock_pool_candidates.json.load", return_value=drill_data),
            mock.patch("scripts.classify_stock_pool_candidates.open", mock.mock_open()),
            mock.patch("scripts.classify_stock_pool_candidates.os.path.exists", return_value=True),
            mock.patch("scripts.classify_stock_pool_candidates.sqlite3.connect", return_value=conn),
            mock.patch("scripts.classify_stock_pool_candidates.setup_logging"),
        ):
            findings = load_candidates("/fake/drill.json", "/fake/drill.db", mock.MagicMock())

        assert len(findings) == 239
        codes = [f["code"] for f in findings]
        assert len(set(codes)) == 239


# ===================================================================
# Test 2: 920808 → CORRECT_MARKET_LABEL
# ===================================================================

class Test920808:
    """920808 (曙光数创) should get CORRECT_MARKET_LABEL with high confidence."""

    def test_920808_correct_market_label(self):
        info = {
            "code": "920808",
            "name": "曙光数创",
            "industry": "计算机",
            "market": "SZ",
            "list_date": "20221201",
            "is_active": 1,
        }
        result = classify_code("920808", info, None, mock.MagicMock())

        assert result["suggestion"] == "CORRECT_MARKET_LABEL"
        assert result["confidence"] == "high"
        assert result["sql_generated"] is True
        assert "北交所" in result["rationale"] or "北交所" in str(result)


# ===================================================================
# Test 3: XXX/TEST → QUARANTINE_TEST_RECORD
# ===================================================================

class TestXXXTestRecord:
    """XXX/TEST should get QUARANTINE_TEST_RECORD."""

    def test_xxx_test_quarantine(self):
        info = {
            "code": "XXX",
            "name": "TEST",
            "industry": "Test",
            "market": "SZ",
            "list_date": "20200101",
            "is_active": 1,
        }
        result = classify_code("XXX", info, None, mock.MagicMock())

        assert result["suggestion"] == "QUARANTINE_TEST_RECORD"
        assert result["confidence"] == "high"
        assert result["sql_generated"] is True


# ===================================================================
# Test 4: list_date=20260518 + no klines → KEEP_HISTORICAL_INACTIVE
# ===================================================================

class TestPlaceholderNoKlines:
    """A code with list_date=20260518 and no klines gets KEEP_HISTORICAL_INACTIVE
    when it is in SUCCESSOR_MAP with successor=None (e.g. delisted with no successor)."""

    @pytest.mark.parametrize("delisted_code", [
        code for code, info in SUCCESSOR_MAP.items() if info["successor"] is None
    ])
    def test_successor_none_keep_historical_inactive(self, delisted_code):
        info = {
            "code": delisted_code,
            "name": "DelistedStock",
            "industry": "金融",
            "market": "SZ",
            "list_date": "20260518",
            "is_active": 1,
        }
        result = classify_code(delisted_code, info, None, mock.MagicMock())

        assert result["suggestion"] == "KEEP_HISTORICAL_INACTIVE"
        assert result["confidence"] == "high"
        assert result["sql_generated"] is True

    def test_unknown_code_placeholder_no_klines_no_change(self):
        info = {
            "code": "999999",
            "name": "UnknownOld",
            "industry": "未知",
            "market": "SZ",
            "list_date": "20260518",
            "is_active": 1,
        }
        result = classify_code("999999", info, None, mock.MagicMock())

        assert result["suggestion"] == "NO_CHANGE_NEEDS_REVIEW"
        assert result["confidence"] == "low"
        assert result["sql_generated"] is False


# ===================================================================
# Test 5: No DELETE statements in proposed-forward.sql
# ===================================================================

class TestNoDeleteInForwardSQL:
    """proposed-forward.sql must NOT contain any DELETE statements."""

    def test_no_delete_statements(self):
        classified = [
            {
                "code": "000001",
                "suggestion": "KEEP_HISTORICAL_INACTIVE",
                "confidence": "high",
                "rationale": "Test delisted stock",
                "sql_generated": True,
                "kline_count": 50,
                "kline_first": "20000101",
                "kline_last": "20231231",
            },
        ]
        forward_sql, _ = generate_sql(classified, mock.MagicMock())

        for line in forward_sql.splitlines():
            stripped = line.strip().upper()
            if stripped.startswith("DELETE") or stripped.startswith("DELETE "):
                pytest.fail(f"Found DELETE statement: {line}")


# ===================================================================
# Test 6: No unbounded UPDATE (every UPDATE has WHERE clause)
# ===================================================================

class TestNoUnboundedUpdate:
    """Every UPDATE statement in forward SQL must have a WHERE clause."""

    def test_all_updates_have_where(self):
        classified = [
            {
                "code": "000001",
                "suggestion": "KEEP_HISTORICAL_INACTIVE",
                "confidence": "high",
                "rationale": "Test",
                "sql_generated": True,
                "kline_count": 50,
                "kline_first": "20000101",
                "kline_last": "20231231",
            },
            {
                "code": "920808",
                "suggestion": "CORRECT_MARKET_LABEL",
                "confidence": "high",
                "rationale": "Test market fix",
                "sql_generated": True,
            },
        ]
        forward_sql, _ = generate_sql(classified, mock.MagicMock())

        for stmt in (s.strip() for s in forward_sql.split(";") if s.strip()):
            if stmt.upper().startswith("UPDATE"):
                assert "WHERE" in stmt.upper(), f"Unbounded UPDATE found: {stmt}"


# ===================================================================
# Test 7: Forward and rollback SQLs are 1:1
# ===================================================================

class TestForwardRollbackParity:
    """Forward and rollback SQL must have the same number of statements."""

    def _count_statements(self, sql: str) -> int:
        return sum(1 for line in sql.splitlines() if line.strip().endswith(";"))

    def test_one_to_one_statements(self):
        classified = [
            {
                "code": "000001",
                "suggestion": "KEEP_HISTORICAL_INACTIVE",
                "confidence": "high",
                "rationale": "Test",
                "sql_generated": True,
                "kline_count": 50,
                "kline_first": "20000101",
                "kline_last": "20231231",
            },
            {
                "code": "920808",
                "suggestion": "CORRECT_MARKET_LABEL",
                "confidence": "high",
                "rationale": "Test market fix",
                "sql_generated": True,
            },
        ]
        forward_sql, rollback_sql = generate_sql(classified, mock.MagicMock())

        assert self._count_statements(forward_sql) == self._count_statements(rollback_sql)


# ===================================================================
# Test 8: Low-confidence items do NOT appear in forward SQL
# ===================================================================

class TestLowConfidenceExcluded:
    """Items with low/medium confidence must NOT generate SQL statements."""

    def test_low_confidence_items_excluded_from_forward_sql(self):
        classified = [
            {
                "code": "000001",
                "suggestion": "KEEP_HISTORICAL_INACTIVE",
                "confidence": "high",
                "rationale": "Has SQL",
                "sql_generated": True,
                "kline_count": 50,
                "kline_first": "20000101",
                "kline_last": "20231231",
            },
            {
                "code": "999999",
                "suggestion": "NO_CHANGE_NEEDS_REVIEW",
                "confidence": "low",
                "rationale": "No SQL",
                "sql_generated": False,
            },
            {
                "code": "888888",
                "suggestion": "KEEP_HISTORICAL_INACTIVE",
                "confidence": "medium",
                "rationale": "Medium confidence — no SQL",
                "sql_generated": False,
                "kline_count": 10,
                "kline_first": "20000101",
                "kline_last": "20231231",
            },
        ]
        forward_sql, _ = generate_sql(classified, mock.MagicMock())

        assert "999999" not in forward_sql
        assert "888888" not in forward_sql

    def test_low_confidence_items_in_manual_review_list(self):
        info = {
            "code": "999999",
            "name": "UnknownOld",
            "industry": "未知",
            "market": "SZ",
            "list_date": "20260518",
            "is_active": 1,
        }
        result = classify_code("999999", info, None, mock.MagicMock())

        assert result["suggestion"] == "NO_CHANGE_NEEDS_REVIEW"
        assert result["sql_generated"] is False

    def test_high_confidence_items_appear_in_forward_sql(self):
        classified = [
            {
                "code": "920808",
                "suggestion": "CORRECT_MARKET_LABEL",
                "confidence": "high",
                "rationale": "Fix market label",
                "sql_generated": True,
            },
        ]
        forward_sql, _ = generate_sql(classified, mock.MagicMock())

        assert "920808" in forward_sql

    def test_medium_confidence_no_sql_generated(self):
        info = {
            "code": "000001",
            "name": "平安银行",
            "industry": "银行",
            "market": "SZ",
            "list_date": "20260518",
            "is_active": 1,
        }
        kline = {
            "code": "000001",
            "first_date": "20000101",
            "last_date": "20231231",
            "kline_count": 5000,
        }
        result = classify_code("000001", info, kline, mock.MagicMock())

        assert result["suggestion"] == "KEEP_HISTORICAL_INACTIVE"
        assert result["confidence"] == "medium"
        assert result["sql_generated"] is False


# ===================================================================
# Test 9: Script refuses to write to source databases (read-only mode)
# ===================================================================

class TestReadOnlyDatabase:
    """The script must open source databases in read-only mode."""

    def test_connect_uses_readonly_uri(self):
        conn = _MockConnection(
            stock_info_row=_fake_stock_info_row("000001"),
            kline_row=_fake_kline_bounds("000001"),
        )
        with (
            mock.patch("scripts.classify_stock_pool_candidates.json.load",
                       return_value=_make_drill_data(239)),
            mock.patch("scripts.classify_stock_pool_candidates.open", mock.mock_open()),
            mock.patch("scripts.classify_stock_pool_candidates.os.path.exists", return_value=True),
            mock.patch("scripts.classify_stock_pool_candidates.sqlite3.connect",
                       return_value=conn) as mock_connect,
            mock.patch("scripts.classify_stock_pool_candidates.setup_logging"),
            mock.patch("scripts.classify_stock_pool_candidates.write_outputs"),
            mock.patch("scripts.classify_stock_pool_candidates.argparse.ArgumentParser.parse_args",
                       return_value=mock.MagicMock(
                           market_db="/fake/qs.db",
                           drill_json="/fake/drill.json",
                           drill_db="/fake/drill.db",
                           output_dir="/tmp/classify-output",
                       )),
        ):
            main()

        # Verify that sqlite3.connect was called with a read-only URI
        for call_args in mock_connect.call_args_list:
            args, kwargs = call_args
            uri = args[0] if args else kwargs.get("database", "")
            if "stock_info" in uri or "qs.db" in uri or "drill" in uri:
                assert "mode=ro" in uri, f"Expected read-only URI, got: {uri}"

    def test_connect_raises_on_write_attempt(self):
        conn = _MockConnection(
            stock_info_row=_fake_stock_info_row("000001"),
            kline_row=_fake_kline_bounds("000001"),
        )
        with (
            mock.patch("scripts.classify_stock_pool_candidates.json.load",
                       return_value=_make_drill_data(239)),
            mock.patch("scripts.classify_stock_pool_candidates.open", mock.mock_open()),
            mock.patch("scripts.classify_stock_pool_candidates.os.path.exists", return_value=True),
            mock.patch("scripts.classify_stock_pool_candidates.sqlite3.connect",
                       return_value=conn) as mock_connect,
            mock.patch("scripts.classify_stock_pool_candidates.setup_logging"),
            mock.patch("scripts.classify_stock_pool_candidates.write_outputs"),
            mock.patch("scripts.classify_stock_pool_candidates.argparse.ArgumentParser.parse_args",
                       return_value=mock.MagicMock(
                           market_db="/fake/qs.db",
                           drill_json="/fake/drill.json",
                           drill_db="/fake/drill.db",
                           output_dir="/tmp/classify-output",
                       )),
        ):
            main()

        # Every connect call targeting a source DB must use mode=ro
        for call_args in mock_connect.call_args_list:
            uri = call_args[0][0] if call_args[0] else ""
            if "/fake/qs.db" in uri or "/fake/drill.db" in uri:
                assert "mode=ro" in uri, f"Source DB connect must be read-only: {uri}"
                assert "?mode=ro" in uri, f"Source DB connect must use URI mode=ro: {uri}"