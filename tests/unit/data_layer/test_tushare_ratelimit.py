"""W1: exact 09-16 minute-limit wording, bounded core retry and fast fallback.

No network/real sleep. The fake provider models a 60-second window; a shortened
backoff mutation must fail the same outcome assertion. Daily/permission quotas
are deliberately NOT reset by this fake clock.
"""
from unittest.mock import Mock

import pytest

from data_layer.sources.tushare_source import TushareSourceAdapter

LIMIT = "抱歉，您访问接口(daily)频率超限(50次/分钟)，具体频次详情：https://tushare.pro/document/1?doc_id=108。"


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr("data_layer.sources.tushare_source.time.sleep", sleep)
    monkeypatch.setattr("random.uniform", lambda low, high: 0.0)
    return now, sleeps


@pytest.mark.parametrize("message", [
    LIMIT, "每分钟最多访问该接口1次", "访问频率超限(50次/分钟)",
    "频 率 超 限（５０ 次／分 钟）", "（５０次／分钟）", "频率超限",
    "ＲＡＴＥ　ＬＩＭＩＴ exceeded", "rate-limit exceeded", "频，率，超，限", "rate\tlimit exceeded",
])
def test_minute_limit_classified(message):
    assert TushareSourceAdapter._is_retryable(Exception(message)) is True


@pytest.mark.parametrize("message", [
    "抱歉，您每天最多访问该接口1次",
    "频率超限(20000次/天)",
    "daily rate limit exceeded",
    "permission denied: rate limit",
    "抱歉，您没有该接口的访问权限，每分钟最多访问0次",
    "token invalid",
    "bad param: ts_code invalid",
    "频率超限（２００００次／天）",
    "ＤＡＩＬＹ　ＲＡＴＥ　ＬＩＭＩＴ exceeded",
    "每日配额不足 rate limit", "quota exceeded: rate limit",
    "权限不足：频 率 超 限", "积分不足 rate limit", "daily-rate-limit exceeded",
])
def test_terminal_error_never_retried(message, clock):
    ad = TushareSourceAdapter()
    exc = Exception(message)
    fn = Mock(side_effect=exc)
    with pytest.raises(Exception) as caught:
        ad._retry_call("daily", fn)
    assert caught.value is exc
    assert fn.call_count == 1
    assert clock[1] == []
    assert ad._is_retryable(exc) is False


def _assert_window_recovery(ad, clock):
    now, sleeps = clock
    payload = [{"date": "2026-09-16", "close": 12.3}]
    calls = []

    def provider():
        calls.append(now[0])
        if now[0] < 60:
            raise Exception(LIMIT)
        return payload

    result, error = None, None
    try:
        result = ad._retry_call("daily", provider)
    except Exception as exc:
        error = exc
    assert error is None, f"minute-window recovery failed: {error}"
    assert result is payload
    assert calls == [0.0, 65.0]
    assert sleeps == [65.0]


def test_exact_limit_recovers_after_window(clock):
    _assert_window_recovery(TushareSourceAdapter(), clock)


def test_short_backoff_mutation_is_caught(clock, monkeypatch):
    ad = TushareSourceAdapter()
    monkeypatch.setattr(ad, "BULK_RATE_LIMIT_BACKOFF_S", 2.0, raising=False)
    with pytest.raises(AssertionError, match="minute-window recovery failed"):
        _assert_window_recovery(ad, clock)


def test_persistent_limit_two_windows_then_raises(clock, caplog):
    ad = TushareSourceAdapter()
    exc = Exception(LIMIT)
    fn = Mock(side_effect=exc)
    with pytest.raises(Exception) as caught:
        ad._retry_call("daily", fn)
    assert caught.value is exc
    assert fn.call_count == 3
    assert clock[1] == [65.0, 65.0]
    assert "attempt=3/3" in caplog.text


def test_daily_basic_rate_limit_fast_cached_failure(clock, caplog):
    ad = TushareSourceAdapter()
    ad._pro = Mock()
    ad._pro.daily_basic.side_effect = Exception(LIMIT)
    assert ad._get_daily_basic("20260916") is None
    assert ad._get_daily_basic("20260916") is None
    ad._pro.daily_basic.assert_called_once_with(trade_date="20260916")
    assert clock[1] == []
    assert "频率超限" in caplog.text


def test_unqualified_rate_limit_does_not_guess_a_window(clock):
    fn = Mock(side_effect=Exception("too many requests"))
    with pytest.raises(Exception, match="too many"):
        TushareSourceAdapter()._retry_call("daily", fn)
    assert fn.call_count == 1
    assert clock[1] == []


def test_success_identity_and_empty_are_not_retried(clock):
    ad = TushareSourceAdapter()
    for payload in [[{"date": "2026-09-16", "close": 12.3}], [], None]:
        fn = Mock(return_value=payload)
        assert ad._retry_call("daily", fn) is payload
        fn.assert_called_once_with()
    assert clock[1] == []


def test_market_date_fields_unchanged_after_recovery(clock):
    ad = TushareSourceAdapter()
    df = Mock(empty=False)
    df.iterrows.return_value = iter([(0, {
        "ts_code": "600001.SH", "trade_date": "20260916", "open": 10,
        "high": 12, "low": 9, "close": 11, "vol": 2, "amount": 3,
        "pre_close": 10, "pct_chg": 10,
    })])
    ad._pro = Mock()
    ad._pro.daily.side_effect = [Exception(LIMIT), df]
    basic = Mock(empty=False)
    basic.iterrows.return_value = iter([(0, {"ts_code": "600001.SH", "turnover_rate": 2.5})])
    ad._pro.daily_basic.return_value = basic
    assert ad.get_daily_market("2026-09-16") == [{
        "code": "600001", "date": "2026-09-16", "open": 10., "high": 12.,
        "low": 9., "close": 11., "volume": 200., "amount": 3000.,
        "pre_close": 10., "change_pct": 10., "turnover_rate": 2.5,
    }]
    assert [call.kwargs for call in ad._pro.daily.call_args_list] == [
        {"trade_date": "20260916"}, {"trade_date": "20260916"}]
    ad._pro.daily_basic.assert_called_once_with(trade_date="20260916")


def test_mixed_network_and_limit_stays_within_attempt_cap(clock):
    fn = Mock(side_effect=[TimeoutError("timeout"), Exception(LIMIT), Exception(LIMIT)])
    ad = TushareSourceAdapter()
    with pytest.raises(Exception, match="频率超限"):
        ad._retry_call("daily", fn)
    assert fn.call_count == ad.BULK_MAX_ATTEMPTS == 3
    assert clock[1] == [2., 65.]
    # Conservative bound: three wallclocks + one minute + largest network sleep.
    worst = ad.BULK_MAX_ATTEMPTS * ad._WALLCLOCK_TIMEOUT_S + sum(clock[1])
    assert worst < 300


@pytest.mark.parametrize("jitter", [0.0, 5.0])
def test_jitter_bounds_and_second_retry_success(clock, monkeypatch, jitter):
    seen = []

    def draw(low, high):
        seen.append((low, high))
        return jitter

    monkeypatch.setattr("random.uniform", draw)
    payload = [{"date": "2026-09-16", "close": 11.0}]
    fn = Mock(side_effect=[Exception(LIMIT), Exception(LIMIT), payload])
    result, error = None, None
    try:
        result = TushareSourceAdapter()._retry_call("daily", fn)
    except Exception as exc:
        error = exc
    assert error is None, f"second minute-window recovery failed: {error}"
    assert result is payload
    assert fn.call_count == 3
    assert seen == [(0.0, 5.0), (0.0, 5.0)]
    assert clock[1] == [65.0 + jitter, 65.0 + jitter]
    assert 3 * TushareSourceAdapter._WALLCLOCK_TIMEOUT_S + sum(clock[1]) <= 320
