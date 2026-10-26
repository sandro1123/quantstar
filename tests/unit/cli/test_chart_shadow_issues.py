# -*- coding: utf-8 -*-
"""#101 切3 issues 日报观察项 RED (TDD) — 影子模式哨兵 (设计稿 §5/§6)

§6 切3 三件之一: issues 日报观察项 — chart_analysis 影子模式可观测面:
  1. 通电观测 (info): 当日有行 → 双录覆盖情况一条 info (影子期日常口径,
     不属于任何回归; 6 周裁决窗内每天可见, 裁决日取数直接 SELECT);
  2. 断电回归 (warning): 当日信号候选非零 (signal_provenance) 但
     chart_analysis 零行 = 读图管道未跑 (表建未通电复燃), 报 warning;
  3. 旧库无表 → 无 issue 不炸 (与 S12 concept 哨兵同语义);
  4. degraded 比例 = 证据面数据, 只观测不判级 (6 周窗内 LLM 通道抖动
     属预期, 不收 issue 刷屏 — 裁决日按证据聚合)。

先例: collect_concept_fallback_regression (R4, 63ee649) — try/except
sqlite3.OperationalError 无表降级 + COLLECTORS 名册注册 + S13 前缀
(S0~S12 已占, 本卡领 S13)。

hermetic: tmp_path 原生 sqlite3 + 生产 DDL 入口建 chart_analysis
(ensure_schema, 不夹具自造列名) + signal_provenance 走 ProvenanceTracker
ctor (56 卡坑先例)。
"""
import sqlite3

from execution_layer.migrations import ensure_schema
from strategy_layer.provenance import ProvenanceTracker

D = "2026-09-03"


def _mk_conn(tmp_path):
    """生产 DDL 入口建 chart_analysis (真迁移器), 不自造列。"""
    from common.database import Database
    db = Database(str(tmp_path / "t.db"))
    db.init_db()
    ensure_schema(db)
    ProvenanceTracker(db)   # signal_provenance 建表入口 (56卡坑)
    from execution_layer.tables import STOCK_POOL_DDL
    db.get_connection().execute(STOCK_POOL_DDL)   # 候选并集第二腿 (切2 同款)
    db.get_connection().commit()
    return db.get_connection(), db


def _chart(conn, code, source, pattern="缩量回踩突破", date=D):
    conn.execute(
        "INSERT INTO chart_analysis (code, date, pattern, source) "
        "VALUES (?,?,?,?)", (code, date, pattern, source))
    conn.commit()


def _signal(conn, code, date=D):
    conn.execute(
        "INSERT INTO signal_provenance (signal_id, code, date, action) "
        "VALUES (?,?,?,?)", (f"s-{code}", code, date, "buy"))
    conn.commit()


# ================================================================ 组1: 通电观测 (info)
class TestWiredObservation:
    def test_wired_day_info_observation(self, tmp_path):
        """当日有行 → 一条 info: 双录/降级比例可观测 (S13 键)"""
        conn, db = _mk_conn(tmp_path)
        _chart(conn, "000001", "atr_floor")
        _chart(conn, "000001", "llm_chart")
        _chart(conn, "000002", "llm_chart", pattern=None)  # degraded
        from cli.issues import collect_chart_shadow
        issues, asof = collect_chart_shadow(conn, D)
        assert len(issues) == 1
        it = issues[0]
        assert it["severity"] == "info"
        assert it["dedup_key"] == f"S13:chart_shadow:{D.replace('-', '')}"
        assert it["evidence"]["atr_floor_rows"] == 1
        assert it["evidence"]["llm_chart_rows"] == 1
        assert it["evidence"]["degraded_rows"] == 1
        conn.close()
        db.close()

    def test_clean_wired_day_no_degraded(self, tmp_path):
        """零降级行 → 同一条 info, degraded=0 (正常态不静默)"""
        conn, db = _mk_conn(tmp_path)
        _chart(conn, "000001", "atr_floor")
        _chart(conn, "000001", "llm_chart")
        from cli.issues import collect_chart_shadow
        issues, _ = collect_chart_shadow(conn, D)
        assert len(issues) == 1
        assert issues[0]["evidence"]["degraded_rows"] == 0
        conn.close()
        db.close()


# ================================================================ 组2: 断电回归 (warning)
class TestBlackoutRegression:
    def test_blackout_day_warning(self, tmp_path):
        """当日有信号候选但 chart_analysis 零行 → warning (表建未通电)"""
        conn, db = _mk_conn(tmp_path)
        _signal(conn, "000001")
        from cli.issues import collect_chart_shadow
        issues, _ = collect_chart_shadow(conn, D)
        assert len(issues) == 1
        assert issues[0]["severity"] == "warning"
        assert "读图" in issues[0]["title_zh"]
        assert issues[0]["evidence"]["candidate_count"] == 1
        conn.close()
        db.close()

    def test_candidates_uses_union_scope(self, tmp_path):
        """候选口径 = 切2 load_candidates 同款 (signal 当日 ∪ pool 未出场)"""
        conn, db = _mk_conn(tmp_path)
        _signal(conn, "000001", date="2026-09-02")     # 非当日 → 不入
        conn.execute(
            "INSERT INTO stock_pool (code, name, status, added_date) "
            "VALUES ('300750', '宁德时代', 'watch', '2026-07-02')")
        conn.commit()
        from cli.issues import collect_chart_shadow
        issues, _ = collect_chart_shadow(conn, D)
        assert len(issues) == 1
        assert issues[0]["evidence"]["candidate_count"] == 1
        assert issues[0]["severity"] == "warning"
        conn.close()
        db.close()

    def test_both_zero_no_issue(self, tmp_path):
        """无信号无读图 (假日/未排程日) → 无 issue (不发空警)"""
        conn, db = _mk_conn(tmp_path)
        from cli.issues import collect_chart_shadow
        issues, asof = collect_chart_shadow(conn, D)
        assert issues == []
        assert asof is None
        conn.close()
        db.close()


# ================================================================ 组3: 无表与注册
class TestRegistration:
    def test_old_db_no_table_no_crash(self, tmp_path):
        """旧库无 chart_analysis → 无 issue 不炸 (不是回归)"""
        conn = sqlite3.connect(str(tmp_path / "old.db"))
        conn.execute("CREATE TABLE other (x TEXT)")
        from cli.issues import collect_chart_shadow
        issues, asof = collect_chart_shadow(conn, D)
        assert issues == [] and asof is None
        conn.close()

    def test_registered_in_collectors(self):
        """挂 COLLECTORS 名册 (R4 哨兵同款注册测试)"""
        from cli.issues import COLLECTORS
        names = [n for n, _ in COLLECTORS]
        assert "chart_analysis_shadow" in names

    def test_asof_from_chart_analysis_created_at(self, tmp_path):
        """asof = 当日 chart_analysis 最新 created_at (表内 DEFAULT)"""
        conn, db = _mk_conn(tmp_path)
        _chart(conn, "000001", "atr_floor")
        from cli.issues import collect_chart_shadow
        issues, asof = collect_chart_shadow(conn, D)
        assert len(issues) == 1
        assert asof is not None and str(asof).startswith("2026-")
        conn.close()
        db.close()
