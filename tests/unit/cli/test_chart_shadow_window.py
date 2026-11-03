# -*- coding: utf-8 -*-
"""#S13-a chart_shadow 时点闸 — 治「每天必误报一次」

## 病
2026-10-28 / 10-29 生产 issues daily 连续报
`S13: 候选 21 只但 chart_analysis 零行`(warning)，但当天 19:05 读图管道
跑完后表里实际有 42/50 行。原判据只看「当日零行」，**不区分小时** ——
日报只要在 19:05 之前生成，当日必然零行 → 结构性误报。

## 药
按「是否已过定论时点」(排程 19:05 + 预算 1200s + 缓冲 → 20:30) 分档:
  未到点 + 零行 + 有候选 → **info**(未到点, 不是故障)
  已过点 + 零行 + 有候选 → **warning**(真断电)
  历史日期 → 一律按已过点判 (那天早该跑完)

## 反向钉
- 注掉时点闸 (恒返回 True) → 未到点用例必须报红;
- 时点闸恒返回 False → 历史日期用例必须报红 (防"永不告警"反向失效)。
"""
import sqlite3

import pytest

from execution_layer.migrations import ensure_schema
from strategy_layer.provenance import ProvenanceTracker

D_TODAY = "2026-10-29"
D_PAST = "2026-10-28"


def _mk_conn(tmp_path):
    from common.database import Database
    db = Database(str(tmp_path / "t.db"))
    db.init_db()
    ensure_schema(db)
    ProvenanceTracker(db)
    from execution_layer.tables import STOCK_POOL_DDL
    db.get_connection().execute(STOCK_POOL_DDL)
    db.get_connection().commit()
    return db.get_connection(), db


def _signal(conn, code, date):
    conn.execute(
        "INSERT INTO signal_provenance (signal_id, code, date, action) "
        "VALUES (?,?,?,?)", (f"s-{code}-{date}", code, date, "buy"))
    conn.commit()


# ============================================================ 组1: 时点闸本身
class TestWindowGate:
    def test_past_date_always_passed(self):
        """历史日期 → 已过点 (无「未到点」概念)"""
        from cli.issues import _chart_shadow_window_passed
        assert _chart_shadow_window_passed(D_PAST, now="08:00") is True

    def test_today_before_done_time_not_passed(self):
        """当日 08:00 (远早于 20:30) → 未到点"""
        from cli.issues import _chart_shadow_window_passed
        from cli.issues import _today
        assert _chart_shadow_window_passed(_today(), now="08:00") is False

    def test_today_after_done_time_passed(self):
        """当日 21:00 (晚于 20:30) → 已到定论时点"""
        from cli.issues import _chart_shadow_window_passed, _today
        assert _chart_shadow_window_passed(_today(), now="21:00") is True

    def test_boundary_exactly_at_done_time(self):
        """边界: 恰好 20:30 → 算已过点 (>=)"""
        from cli.issues import (_chart_shadow_window_passed, _today,
                                CHART_SHADOW_DONE_HHMM)
        assert _chart_shadow_window_passed(
            _today(), now=CHART_SHADOW_DONE_HHMM) is True

    def test_future_date_not_passed(self):
        """未来日期 → 不算断电"""
        from cli.issues import _chart_shadow_window_passed
        assert _chart_shadow_window_passed("2099-01-01", now="23:00") is False


# ============================================================ 组2: 采集器分档
class TestCollectorTiering:
    def test_before_window_is_info_not_warning(self, tmp_path, monkeypatch):
        """未到点 + 有候选 + 零行 → info (不再误报 warning)"""
        conn, db = _mk_conn(tmp_path)
        _signal(conn, "000001", D_TODAY)
        import cli.issues as iss
        monkeypatch.setattr(iss, "_chart_shadow_window_passed", lambda d, now=None: False)

        issues, _ = iss.collect_chart_shadow(conn, D_TODAY)

        assert len(issues) == 1
        assert issues[0]["severity"] == "info", (
            "未到点不该报 warning — 这正是每天误报的根因")
        assert "未到点" in issues[0]["title_zh"]
        assert issues[0]["evidence"]["window_passed"] is False
        conn.close(); db.close()

    def test_after_window_still_warns(self, tmp_path, monkeypatch):
        """已过点 + 有候选 + 零行 → warning (真断电, 不能被时点闸掩盖)"""
        conn, db = _mk_conn(tmp_path)
        _signal(conn, "000001", D_TODAY)
        import cli.issues as iss
        monkeypatch.setattr(iss, "_chart_shadow_window_passed", lambda d, now=None: True)

        issues, _ = iss.collect_chart_shadow(conn, D_TODAY)

        assert len(issues) == 1
        assert issues[0]["severity"] == "warning"
        assert "断电" in issues[0]["title_zh"]
        assert issues[0]["evidence"]["window_passed"] is True
        conn.close(); db.close()

    def test_pending_and_real_blackout_have_distinct_dedup_keys(self, tmp_path, monkeypatch):
        """未到点与真断电的 dedup_key 必须不同 — 否则台账会把两者当同一件事"""
        conn, db = _mk_conn(tmp_path)
        _signal(conn, "000001", D_TODAY)
        import cli.issues as iss

        monkeypatch.setattr(iss, "_chart_shadow_window_passed", lambda d, now=None: False)
        pending, _ = iss.collect_chart_shadow(conn, D_TODAY)
        monkeypatch.setattr(iss, "_chart_shadow_window_passed", lambda d, now=None: True)
        blackout, _ = iss.collect_chart_shadow(conn, D_TODAY)

        assert pending[0]["dedup_key"] != blackout[0]["dedup_key"]
        conn.close(); db.close()

    def test_has_rows_still_info_regardless_of_window(self, tmp_path):
        """有行 → 永远是 info (时点闸不干扰正常态)"""
        conn, db = _mk_conn(tmp_path)
        conn.execute("INSERT INTO chart_analysis (code, date, pattern, source) "
                     "VALUES ('000001', ?, 'x', 'atr_floor')", (D_TODAY,))
        conn.commit()
        from cli.issues import collect_chart_shadow
        issues, _ = collect_chart_shadow(conn, D_TODAY)
        assert len(issues) == 1 and issues[0]["severity"] == "info"
        conn.close(); db.close()

    def test_no_candidate_no_issue(self, tmp_path):
        """无候选 (假日) → 什么都不报, 时点无关"""
        conn, db = _mk_conn(tmp_path)
        from cli.issues import collect_chart_shadow
        issues, _ = collect_chart_shadow(conn, D_TODAY)
        assert issues == []
        conn.close(); db.close()


# ============================================================ 组3: 生产回归
class TestProductionRegression:
    def test_reproduces_2026_10_28_incident(self, tmp_path, monkeypatch):
        """重放 10-28 现场: 候选非零 + 零行 + 日报在 08:00 生成 → 不得 warning

        这是本条 issue 的来源场景; 修好后必须 info。
        """
        conn, db = _mk_conn(tmp_path)
        for i in range(21):          # 实测 10-29 候选 21 只
            _signal(conn, f"{i:06d}", D_TODAY)
        import cli.issues as iss
        monkeypatch.setattr(iss, "_chart_shadow_window_passed", lambda d, now=None: False)

        issues, _ = iss.collect_chart_shadow(conn, D_TODAY)
        assert len(issues) == 1
        assert issues[0]["severity"] != "warning", (
            "10-28 误报的正是这条 — 未到点时必须不是 warning")
        assert issues[0]["evidence"]["candidate_count"] == 21
        conn.close(); db.close()
