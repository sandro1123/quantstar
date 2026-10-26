"""Tests for common/utils.py"""
import pytest
from common.utils import generate_trace_id, bj_now, bj_date, from_bj_str


def test_generate_trace_id_length():
    tid = generate_trace_id()
    assert len(tid) == 12


def test_generate_trace_id_unique():
    tids = {generate_trace_id() for _ in range(100)}
    assert len(tids) == 100


def test_bj_date_format():
    date = bj_date()
    assert len(date) == 10


def test_bj_now_format():
    now = bj_now()
    assert " " in now
    parts = now.split(" ")
    assert len(parts) == 2


def test_from_bj_str():
    from datetime import datetime
    dt = from_bj_str("2026-07-18 14:30:00")
    assert isinstance(dt, datetime)
    assert dt.year == 2026
    assert dt.month == 7
    assert dt.day == 18