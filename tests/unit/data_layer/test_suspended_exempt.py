# -*- coding: utf-8 -*-
"""停牌/退化票 DDP 误报豁免 — TDD RED 测试面 (feat/suspended-exempt, 10-20)。

派单 sm-3973b04c1b434e3d + 方案稿核准 sm-6ba06400cab7416f。
设计 (三层):
  L1 纯库内判据: 近60天无「有效行」(非零) 替代 无「任何行」; 末行退化识别
  L2 suspended_codes 表: auto入表/非零行自动出表/manual保护不被auto删
  L3 显形: 命中豁免仍写 unknown_gap + metadata exempt 计数 (防假绿)

安全前提 (统筹核定写进判据说明): 豁免判据不靠「全源空」而靠库内末行形态 —
  源故障当天该票最后一条仍是非零真值行 (故障发生在今天, 历史是真的);
  真停牌最后一条是全0退化行 ⇒ 真源故障当天不会被误豁免。
"""
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta

import pytest

LOOKBACK = 60
QDATE = "2026-10-19"


def _db_with_klines(rows):
    """rows: [(code, date, volume, amount, close)] → tmp sqlite conn。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE daily_klines (
        code TEXT, date TEXT, volume REAL, amount REAL, close REAL)""")
    conn.executemany("INSERT INTO daily_klines VALUES (?,?,?,?,?)", rows)
    conn.commit()
    return conn, path


class TestL1EffectiveRowCriteria:
    """L1: 「近60天无有效行」替代「无任何行」+ 末行退化识别。"""

    def test_degenerate_last_row_is_expected_empty(self):
        """920305 形态: 近60天仅1行且全0 → expected_empty=True (不判 partial)。"""
        from data_layer.pipelines.kline import _fetch_is_expected_empty
        cutoff = (datetime.strptime(QDATE, "%Y-%m-%d") - timedelta(days=LOOKBACK)).strftime("%Y-%m-%d")
        rows = [("920305", "2026-09-22", 0.0, 0.0, 0.0)]
        conn, path = _db_with_klines(rows)
        try:
            assert _fetch_is_expected_empty(conn, "920305", QDATE) is True, (
                "末行全0退化 = 预期空, 不该判 partial")
        finally:
            conn.close(); os.unlink(path)

    def test_active_last_row_is_source_failure(self):
        """源故障形态: 最后一条仍是非零真值行 → False (照旧判 partial)。"""
        from data_layer.pipelines.kline import _fetch_is_expected_empty
        rows = [("300001", "2026-10-16", 100.0, 5000.0, 10.0)]
        conn, path = _db_with_klines(rows)
        try:
            assert _fetch_is_expected_empty(conn, "300001", QDATE) is False, (
                "历史真+今天挂 = 源故障, 必须判 partial (安全前提)")
        finally:
            conn.close(); os.unlink(path)

    def test_mixed_history_with_recent_nonzero_is_active(self):
        """混合形态 (000016: 7非零+9全0, 末行全0) → 末行判定输给「有过非零」。
        等等 — 这是方案未决点: 000016 末行全0但历史有非零, 到底算停牌还是活跃?
        生产实测 000016 末行全0=2026-10-19 (今天), 形态与 920305 同族。
        本钉锁: 末行全0 → expected_empty=True (与 920305 同判, 不按历史有无非零)。"""
        from data_layer.pipelines.kline import _fetch_is_expected_empty
        rows = [("000016", "2026-08-21", 100.0, 5000.0, 10.0)]  # 有过非零
        rows += [("000016", f"2026-09-{d:02d}", 0.0, 0.0, 0.0) for d in range(10, 20)]
        rows += [("000016", "2026-10-19", 0.0, 0.0, 0.0)]  # 末行全0
        conn, path = _db_with_klines(rows)
        try:
            assert _fetch_is_expected_empty(conn, "000016", QDATE) is True
        finally:
            conn.close(); os.unlink(path)

    def test_sporadic_zero_but_active_tail_still_fails(self):
        """B形态 (000582: 零星全0行但末行真值) → False (源抖动照旧报, 判据独立性)。"""
        from data_layer.pipelines.kline import _fetch_is_expected_empty
        rows = [("000582", "2026-09-09", 0.0, 0.0, 0.0),    # 零星退化
                ("000582", "2026-10-09", 0.0, 0.0, 0.0),
                ("000582", "2026-10-19", 24692568.0, 221031902.0, 8.96)]  # 末行真值
        conn, path = _db_with_klines(rows)
        try:
            assert _fetch_is_expected_empty(conn, "000582", QDATE) is False
        finally:
            conn.close(); os.unlink(path)

    def test_no_rows_at_all_still_expected(self):
        """原判据兼容: 完全无行 → True (60天无数据原语义不变)。"""
        from data_layer.pipelines.kline import _fetch_is_expected_empty
        conn, path = _db_with_klines([])
        try:
            assert _fetch_is_expected_empty(conn, "920999", QDATE) is True
        finally:
            conn.close(); os.unlink(path)

    def test_conn_none_conservative_false(self):
        """conn=None 保守返 False (宁报错不漏报, 原语义不变)。"""
        from data_layer.pipelines.kline import _fetch_is_expected_empty
        assert _fetch_is_expected_empty(None, "920305", QDATE) is False


class TestL2SuspendedTable:
    """L2: suspended_codes 表 — auto入表/非零自动出表/manual保护。"""

    def _table_conn(self):
        from data_layer.pipelines.kline import _ensure_suspended_table
        conn, path = _db_with_klines([])
        _ensure_suspended_table(conn)
        return conn, path

    def test_ddl_creates_table(self):
        conn, path = self._table_conn()
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(suspended_codes)")]
            assert "code" in cols and "source" in cols and "note" in cols, cols
        finally:
            conn.close(); os.unlink(path)

    def test_auto_upsert_and_recovery_delete(self):
        """auto入表 + 非零行出现自动出表 (自愈靠新数据不靠等)。"""
        from data_layer.pipelines.kline import (
            _ensure_suspended_table, _suspended_mark_auto, _suspended_maybe_recover)
        conn, path = self._table_conn()
        try:
            _suspended_mark_auto(conn, "920305", "2026-09-22", QDATE)
            n = conn.execute("SELECT COUNT(*) FROM suspended_codes WHERE code='920305'").fetchone()[0]
            assert n == 1, "检出退化末行 → auto 入表"
            # 恢复: 该码出现非零行 → 出表
            conn.execute("INSERT INTO daily_klines VALUES ('920305','2026-10-20',100.0,5000.0,1.0)")
            conn.commit()
            _suspended_maybe_recover(conn, "920305")
            n = conn.execute("SELECT COUNT(*) FROM suspended_codes WHERE code='920305'").fetchone()[0]
            assert n == 0, "非零行出现 → 自动出表 (自愈)"
        finally:
            conn.close(); os.unlink(path)

    def test_manual_not_deleted_by_recovery(self):
        """manual 挂的不被 auto 恢复删 (人工判断不被自动逻辑覆盖)。"""
        from data_layer.pipelines.kline import (
            _ensure_suspended_table, _suspended_mark_manual, _suspended_maybe_recover)
        conn, path = self._table_conn()
        try:
            _suspended_mark_manual(conn, "920305", "长期停牌核查")
            conn.execute("INSERT INTO daily_klines VALUES ('920305','2026-10-20',100.0,5000.0,1.0)")
            conn.commit()
            _suspended_maybe_recover(conn, "920305")
            row = conn.execute(
                "SELECT source, note FROM suspended_codes WHERE code='920305'").fetchone()
            assert row is not None and row[0] == "manual", "manual 不被 auto 恢复删"
            assert row[1], "manual 必须带 note (硬要求)"
        finally:
            conn.close(); os.unlink(path)

    def test_is_suspended_lookup(self):
        """查询面: 消费方查表命中 → 豁免; 未命中 → 不豁免。"""
        from data_layer.pipelines.kline import (
            _ensure_suspended_table, _suspended_mark_auto, _is_suspended)
        conn, path = self._table_conn()
        try:
            _suspended_mark_auto(conn, "920305", "2026-09-22", QDATE)
            assert _is_suspended(conn, "920305") is True
            assert _is_suspended(conn, "300001") is False
        finally:
            conn.close(); os.unlink(path)
