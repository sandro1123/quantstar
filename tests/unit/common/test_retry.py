"""Tests for common/retry.py"""
import time
import pytest
from common.retry import RetryPolicy, retry_with_backoff


def test_retry_policy_defaults():
    policy = RetryPolicy()
    assert policy.max_retries == 3
    assert policy.backoff_base == 2.0
    assert policy.backoff_max == 30.0
    assert policy.jitter == 0.1


def test_retry_policy_custom():
    policy = RetryPolicy(max_retries=2, backoff_base=1.0)
    assert policy.max_retries == 2
    assert policy.backoff_base == 1.0


def test_retry_success_first_try():
    call_count = 0

    def success():
        nonlocal call_count
        call_count += 1
        return "ok"

    result = retry_with_backoff(success, RetryPolicy(max_retries=3))
    assert result == "ok"
    assert call_count == 1


def test_retry_fails_then_succeeds():
    call_count = 0

    def fails_then_ok():
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise ConnectionError("timeout")
        return "ok"

    result = retry_with_backoff(fails_then_ok, RetryPolicy(max_retries=3, backoff_base=0.01))
    assert result == "ok"
    assert call_count == 3


def test_retry_all_fail():
    call_count = 0

    def always_fail():
        nonlocal call_count
        call_count += 1
        raise ConnectionError("timeout")

    with pytest.raises(ConnectionError):
        retry_with_backoff(always_fail, RetryPolicy(max_retries=2, backoff_base=0.01))
    assert call_count == 2


def test_non_retryable_raises_immediately():
    call_count = 0

    def raise_value_error():
        nonlocal call_count
        call_count += 1
        raise ValueError("bad data")

    with pytest.raises(ValueError):
        retry_with_backoff(raise_value_error, RetryPolicy(max_retries=3, backoff_base=0.01))
    assert call_count == 1