"""Tests for common/calendar.py"""
import pytest
from common.calendar import TradingCalendar


def test_weekend_not_trading():
    cal = TradingCalendar()
    assert cal.is_trading_day("2026-07-18") is False
    assert cal.is_trading_day("2026-07-19") is False


def test_weekday_is_trading():
    cal = TradingCalendar()
    assert cal.is_trading_day("2026-07-20") is True
    assert cal.is_trading_day("2026-07-17") is True


def test_next_trading_day():
    cal = TradingCalendar()
    next_day = cal.next_trading_day("2026-07-17")
    assert next_day == "2026-07-20"


def test_next_trading_day_from_weekend():
    cal = TradingCalendar()
    next_day = cal.next_trading_day("2026-07-18")
    assert next_day == "2026-07-20"


def test_trading_days_in_range():
    cal = TradingCalendar()
    days = cal.trading_days_in_range("2026-07-20", "2026-07-24")
    assert len(days) == 5
    assert days[0] == "2026-07-20"
    assert days[-1] == "2026-07-24"