"""Tests for common/errors.py"""
import pytest
from common.errors import QuantStarError, ErrorSeverity


def test_quant_star_error_basic():
    err = QuantStarError("data_layer", ErrorSeverity.CRITICAL, "trace123", False)
    assert err.layer == "data_layer"
    assert err.severity == ErrorSeverity.CRITICAL
    assert err.trace_id == "trace123"
    assert err.recoverable is False
    assert err.suggestion is None


def test_quant_star_error_with_suggestion():
    err = QuantStarError(
        "config_layer", ErrorSeverity.WARNING, "trace456", True,
        suggestion="检查 settings.yaml 是否存在"
    )
    assert err.suggestion == "检查 settings.yaml 是否存在"


def test_quant_star_error_str():
    err = QuantStarError("data_layer", ErrorSeverity.CRITICAL, "trace123", False)
    s = str(err)
    assert "data_layer" in s
    assert "CRITICAL" in s
    assert "trace123" in s


def test_error_severity_values():
    assert ErrorSeverity.CRITICAL.value == "critical"
    assert ErrorSeverity.WARNING.value == "warning"
    assert ErrorSeverity.INFO.value == "info"


def test_quant_star_error_default_recoverable():
    err = QuantStarError("test", ErrorSeverity.INFO, "t1", True)
    assert err.recoverable is True