# -*- coding: utf-8 -*-
"""ActionCandidatesSection 差异行 TDD (09-28 派单, 统筹裁 a: action_type 组口径)。

口径: 新进=add_to_watch+status_change(→pending_entry) /
移出=remove_from_watch+cancel_entry (剔除 skip_entry/skip_exit 负面跳过) /
调价=set_entry_range。previous_value/new_value 全 NULL (09-24/25 实查) 精筛无锚。
"""
import sqlite3
from unittest.mock import MagicMock, patch

import pytest


def _conn(rows):
    c = sqlite3.connect(":memory:")
    c.execute("""CREATE TABLE action_records (
        id INTEGER PRIMARY KEY, action_type TEXT, reason TEXT, created_at TEXT)""")
    for at, reason, ts in rows:
        c.execute("INSERT INTO action_records (action_type, reason, created_at) "
                  "VALUES (?, ?, ?)", (at, reason, ts))
    return c


def _diff(conn, as_of="2026-09-25"):
    from report_layer.sections.action_candidates import _diff_line
    return _diff_line(conn, as_of)


# 钉①: 昨日实数口径 (09-24 样: 新进 6 / 移出 0 / 调价 3)
def test_diff_counts_yesterday():
    rows = [
        ("add_to_watch", "入观察", "2026-09-24 09:00"),
        ("add_to_watch", "入观察", "2026-09-24 09:01"),
        ("add_to_watch", "入观察", "2026-09-24 09:02"),
        ("status_change", "状态变更 → pending_entry", "2026-09-24 09:03"),
        ("status_change", "状态变更 → pending_entry", "2026-09-24 09:04"),
        ("status_change", "状态变更 → pending_entry", "2026-09-24 09:05"),
        ("set_entry_range", "调价", "2026-09-24 09:06"),
        ("set_entry_range", "调价", "2026-09-24 09:07"),
        ("set_entry_range", "调价", "2026-09-24 09:08"),
    ]
    got = _diff(_conn(rows), "2026-09-25")
    assert got == "较昨日：新进 6 只｜移出 0 只｜调价 3 只", got


# 钉②: 移出精筛 — remove_from_watch+cancel_entry 计入, skip_entry/skip_exit 剔除
def test_moved_out_excludes_negative_skips():
    rows = [
        ("remove_from_watch", "出观察", "2026-09-24 09:00"),
        ("cancel_entry", "取消入场", "2026-09-24 09:01"),
        ("skip_entry", "rr 不足跳过", "2026-09-24 09:02"),   # 负面跳过不计
        ("skip_exit", "未达退出跳过", "2026-09-24 09:03"),   # 负面跳过不计
    ]
    got = _diff(_conn(rows), "2026-09-25")
    assert "移出 2 只" in got, f"skip_entry/skip_exit 不得计入移出: {got}"


# 钉③: status_change 只计 →pending_entry, 其他方向不计入新进
def test_status_change_only_pending_entry():
    rows = [
        ("status_change", "状态变更 → pending_entry", "2026-09-24 09:00"),
        ("status_change", "状态变更 → active", "2026-09-24 09:01"),  # 非入场向不计
    ]
    got = _diff(_conn(rows), "2026-09-25")
    assert "新进 1 只" in got, f"只计 →pending_entry: {got}"


# 钉④: 昨日无行 / 全零差异 → None (不渲染不静默当零)
def test_no_diff_returns_none():
    assert _diff(_conn([]), "2026-09-25") is None
    rows = [("advisory_state", "投顾状态", "2026-09-24 09:00")]  # 非差异类
    assert _diff(_conn(rows), "2026-09-25") is None
