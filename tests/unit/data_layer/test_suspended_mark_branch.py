# -*- coding: utf-8 -*-
"""TDD RED 面: 我的卡 auto 入表挂错分支 — 6 只漏检。
现象 (d1_recheck 零编码复核, JOIN max(date) 口径):
  末行全0 的票 = 7 只 (920305 + 000016/002388/002717/002731/301314/301468)
  suspended_codes 只进了 1 只 (920305, 因它源返空走 err 分支)
  另 6 只源返了记录 (全0行) 走 records 分支 ⇒ 判据说退化但没入表
修法: auto 入表提到两分支公共位; 入表判据仍锚「末行显式全 0」不放宽。
"""
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta

import pytest

DATE = "2026-10-20"


def _db_with_klines(rows):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE daily_klines (
        code TEXT, date TEXT, volume REAL, amount REAL, close REAL)""")
    conn.executemany("INSERT INTO daily_klines VALUES (?,?,?,?,?)", rows)
    conn.commit()
    return conn, path


class TestAutoMarkOnRecordsBranch:
    """核心钉: 源返了全 0 记录 (records 分支) ⇒ 也必须入表。"""

    def test_records_branch_all_zero_row_gets_marked(self):
        """6 只形态: 源返了记录但记录是全 0 → 应入 suspended_codes。"""
        from data_layer.pipelines.kline import (
            _ensure_suspended_table, _suspended_mark_auto, _row_is_degenerate)
        conn, path = _db_with_klines([
            ("000016", "2026-10-19", 100.0, 5000.0, 10.0),   # 历史有真值行
            ("000016", DATE, 0.0, 0.0, 0.0),                  # 今日返了全 0 记录
        ])
        try:
            _ensure_suspended_table(conn)
            # 末行形态确认
            last = conn.execute(
                "SELECT volume, amount, close FROM daily_klines WHERE code=? "
                "ORDER BY date DESC LIMIT 1", ("000016",)).fetchone()
            assert _row_is_degenerate(*last) is True, "末行确为退化"
            # 修后行为: records 分支也要入表
            from data_layer.pipelines.kline import _maybe_mark_suspended
            _maybe_mark_suspended(conn, "000016", DATE)
            n = conn.execute(
                "SELECT COUNT(*) FROM suspended_codes WHERE code='000016'").fetchone()[0]
            assert n == 1, "records 分支返全 0 行 ⇒ 该码应入表 (本次修复点)"
        finally:
            conn.close(); os.unlink(path)

    def test_records_branch_nonzero_row_not_marked(self):
        """反向钉: 源返了非零真值行 ⇒ 不该入表 (判据不放宽)。"""
        from data_layer.pipelines.kline import (
            _ensure_suspended_table, _maybe_mark_suspended)
        conn, path = _db_with_klines([
            ("300001", DATE, 100.0, 5000.0, 10.0),   # 今日真值行
        ])
        try:
            _ensure_suspended_table(conn)
            _maybe_mark_suspended(conn, "300001", DATE)
            n = conn.execute(
                "SELECT COUNT(*) FROM suspended_codes WHERE code='300001'").fetchone()[0]
            assert n == 0, "非零行 ⇒ 不入表 (判据不放宽)"
        finally:
            conn.close(); os.unlink(path)

    def test_null_columns_not_degenerate(self):
        """反向钉: 末行缺列 (NULL) ⇒ 不判退化 (NULL≠0, 不得放宽到 NULL)。"""
        from data_layer.pipelines.kline import _row_is_degenerate
        assert _row_is_degenerate(None, None, None) is False
        assert _row_is_degenerate(0.0, 0.0, 0.0) is True
        assert _row_is_degenerate(0.0, 0.0, 10.0) is False

    def test_sentinel_row_not_degenerate(self):
        """反向钉: 哨兵行 (volume=amount=2^-127) ⇒ 不判退化 (不被入表豁免)。"""
        from data_layer.pipelines.kline import _row_is_degenerate
        s = 5.877471754111438e-39
        assert _row_is_degenerate(s, s, s) is False

    def test_existing_manual_row_not_overwritten(self):
        """manual 行不被 auto 覆盖 (修法不动 manual 保护语义)。"""
        from data_layer.pipelines.kline import (
            _ensure_suspended_table, _suspended_mark_manual, _maybe_mark_suspended)
        conn, path = _db_with_klines([
            ("000016", DATE, 0.0, 0.0, 0.0),
        ])
        try:
            _ensure_suspended_table(conn)
            _suspended_mark_manual(conn, "000016", "人工挂: 长期停牌核查")
            _maybe_mark_suspended(conn, "000016", DATE)
            row = conn.execute(
                "SELECT source, note FROM suspended_codes WHERE code='000016'").fetchone()
            assert row[0] == "manual", "manual 行不被 auto 覆盖"
            assert row[1] == "人工挂: 长期停牌核查", "manual note 保留"
        finally:
            conn.close(); os.unlink(path)
