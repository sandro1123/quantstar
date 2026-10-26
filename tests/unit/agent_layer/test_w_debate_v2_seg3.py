# -*- coding: utf-8 -*-
"""AG-DEBATE-V2 第三段 — invalidation 全链消费验证 (RED 先行)。

施工面 (统筹 sm-ebdfa35a GO + D 席呈批 sm-c1a1b847):
  A 件【落库面】_run_debate 出口把 expert_reports 的 invalidation_conditions
     逐条落 agent_invalidation_warnings 新表 — 跨腿时序解耦
     (16:00 辩论落库 → 次日 15:00 收盘轮查表消费)。
  B 件【消费面】ex4_cli_scan.advisor_segment 改调 invalidation_bridge 的
     advise_with_invalidation (查表装配 expert_reports), 三态判定零改动,
     advisory_state 留痕 reason 追加警示摘要。

RED 锚:
  ① A: 表 DDL 不存在 → _run_debate 落库调用点不存在 → 落库行数为 0 (红)
  ② B: ex4 调原版 advise (非桥版) → reason 无警示摘要面 (红)
  ③ 零回归锚: 无警示行时 reason 与原版逐字等价

三重证:
  ① 调用点: agent_layer._run_debate 出口 INSERT / ex4_cli_scan 桥版调用
  ② 入口可达: 假库全链可跑 (本文件 fixture)
  ③ 落痕有迹: 表行 + advisory_state reason 面可查
"""
import os
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_root))
sys.path.insert(0, str(_root / "src"))

# ---------------------------------------------------------------------------
# 假库 (内存, 真 schema 面 — agent_invalidation_warnings 须由 tables.py 建)
# ---------------------------------------------------------------------------

@pytest.fixture
def mem_db():
    """内存库 + 正典表 (stock_pool/action_records 由 production DDL 建)。"""
    from common.database import Database
    db = Database(":memory:")

    # agent_invalidation_warnings 正典 DDL 位置 = execution_layer/tables.py
    # (init_execution_tables = 同文件正典建表入口, ExecutionLayer.start 同款)
    from execution_layer.tables import init_execution_tables
    init_execution_tables(db.get_connection())
    # action_records 留痕面 (advisory_state 消费落点)
    db.get_connection().execute(
        "CREATE TABLE IF NOT EXISTS action_records ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, action_type TEXT, "
        "action_detail TEXT, price REAL, previous_value TEXT, new_value TEXT, "
        "reason TEXT, source TEXT NOT NULL CHECK (source IN "
        "('strategy','agent','execution','manual')), "
        "created_at TEXT NOT NULL DEFAULT (datetime('now','+8 hours')))")
    db.get_connection().commit()

    conn = db.get_connection()
    # 最小种子: 一只 watch 票 (ex4 消费面用)
    conn.execute(
        "INSERT INTO stock_pool (code, name, status, source, added_date) "
        "VALUES ('000001', '平安银行', 'watch', 'strategy', '2026-09-15')")
    conn.execute(
        "INSERT INTO stock_pool (code, name, status, source, added_date, "
        "entry_low, entry_high) "
        "VALUES ('600152', '维科技术', 'watch', 'strategy', '2026-09-15', "
        "8.04, 8.32)")
    conn.commit()
    yield db
    try:
        db.get_connection().close()
    except Exception:
        pass


def _mk_report(expert="tech", conds=("跌破 9.5 支撑线", "量能萎缩 50%")):
    """伪 ExpertReport — 只带桥消费的字段面。"""
    return SimpleNamespace(
        expert_name=expert, score=0.5, confidence=0.8,
        key_points=["k1"], invalidation_conditions=list(conds),
        tier_used="rules")


# ---------------------------------------------------------------------------
# A 件: 落库面 — _run_debate 出口落表
# ---------------------------------------------------------------------------

class TestSeg3ALedger:
    """A: expert 失效条件出层落库 (裁决后不再内存丢弃)。"""

    def test_a1_run_debate_persists_invalidation_conditions(self, mem_db, monkeypatch):
        """RED: 两轮辩论后 expert 失效条件须落 agent_invalidation_warnings。

        调用点=agent_layer._run_debate 出口; 落库面=每条件一行。
        直调 _run_debate (router 会因 requires_llm 离线降级 reroute,
        落库点锚在 _run_debate 本体 — 精准单测面)。
        注: invalidation_conditions 仅 LLM 档生成 (规则腿 default 空 —
        产品语义正确), 离线测试以 stub 专家注入条件, 锚落库点非专家生成面。
        """
        import agent_layer.core.agent_layer as al_mod
        from agent_layer.core.agent_layer import AgentLayer
        from agent_layer.core.models import AgentRequest

        class _StubExpert:
            name = "stub_tech"
            def analyze(self, data):
                r = _mk_report("stub_tech",
                               ("跌破 9.5 支撑线", "量能萎缩 50%"))
                r.tier_used = "llm"
                return r

        monkeypatch.setattr(al_mod, "ALL_EXPERTS", [_StubExpert])
        al = AgentLayer(database=mem_db)
        req = AgentRequest(
            task="stock_analysis", depth="deep",
            input={"code": "600152", "factor_scores": {"momentum_20d": 0.3},
                   "date": "2026-09-15"})
        resp = al._run_debate(req)
        assert resp is not None, "_run_debate 须可跑 (入口可达证, stub 专家)"
        n = mem_db.get_connection().execute(
            "SELECT COUNT(*) FROM agent_invalidation_warnings").fetchone()[0]
        # 两轮 × 2 条件 = 4 行 (每轮每条件一行, 第 2 轮同条件再落)
        assert n >= 2, (
            "RED: expert 失效条件必须落 agent_invalidation_warnings "
            "(_run_debate 出口逐条落库), 不得裁决后内存丢弃")
        # 行面: code/date/expert_name/condition 四列有值
        row = mem_db.get_connection().execute(
            "SELECT code, date, expert_name, condition "
            "FROM agent_invalidation_warnings LIMIT 1").fetchone()
        assert row[0] == "600152" and row[1] == "2026-09-15" and row[3], (
            f"落库行四列须真值: {tuple(row)}")

    def test_a2_table_in_canonical_ddl(self, mem_db):
        """RED: 表必须在正典 DDL (execution_layer/tables.py) — 干净建库即在。"""
        conn = mem_db.get_connection()
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        assert "agent_invalidation_warnings" in names, (
            "RED: agent_invalidation_warnings 须入 execution_layer/tables.py "
            "正典 DDL — 干净检出即可建 (不依赖 runtime ensure)")

    def test_a3_ledger_degrade_not_crash(self, mem_db, monkeypatch):
        """RED: 落库失败 (表缺) → warning 不炸主链 (铁律 #12 可观测降级)。"""
        conn = mem_db.get_connection()
        conn.execute("DROP TABLE agent_invalidation_warnings")
        conn.commit()
        from agent_layer.core.agent_layer import AgentLayer
        from agent_layer.core.models import AgentRequest
        al = AgentLayer(database=mem_db)
        req = AgentRequest(
            task="stock_analysis", depth="deep",
            input={"code": "600152", "factor_scores": {}, "date": "2026-09-15"})
        resp = al._run_debate(req)  # 不得 raise
        assert resp is not None, "表缺时主链照跑 (降级不炸)"


# ---------------------------------------------------------------------------
# B 件: 消费面 — ex4 桥版通电
# ---------------------------------------------------------------------------

class TestSeg3BConsumer:
    """B: ex4 收盘轮消费表内警示, reason 追加摘要。"""

    def test_b1_advisor_segment_reads_ledger(self, mem_db):
        """RED: 表有警示行 → advisory_state reason 含警示摘要面。"""
        conn = mem_db.get_connection()
        conn.execute(
            "INSERT INTO agent_invalidation_warnings "
            "(code, date, expert_name, condition) VALUES "
            "('600152', '2026-09-15', 'tech', '跌破 9.5 支撑线')")
        conn.commit()
        from execution_layer.scripts.ex4_cli_scan import advisor_segment
        advisor_segment(mem_db, today="2026-09-15", now_hhmm="15:00")
        rows = conn.execute(
            "SELECT reason FROM action_records WHERE action_type='advisory_state' "
            "AND code='600152'").fetchall()
        assert rows, "advisory_state 留痕须存在 (桥版通电后)"
        reason = rows[0][0]
        assert "失效警示" in reason, (
            f"RED: reason 须含警示摘要面, got: {reason}")
        assert "跌破 9.5 支撑线" in reason, "警示条目文本须可见"

    def test_b2_no_warnings_reason_unchanged(self, mem_db):
        """零回归锚: 表无警示行 → reason 与原版逐字等价。"""
        from execution_layer.scripts.ex4_cli_scan import advisor_segment
        advisor_segment(mem_db, today="2026-09-15", now_hhmm="15:00")
        conn = mem_db.get_connection()
        rows = conn.execute(
            "SELECT reason FROM action_records WHERE action_type='advisory_state' "
            "AND code='600152'").fetchall()
        assert rows, "留痕须在"
        reason = rows[0][0]
        # 原版语义: advise() 出的 state: reason 前缀面 (600152 无 lp → wait_watch)
        assert reason.startswith("wait_watch:"), (
            f"三态判定零改动: 无警示时 reason 与原版同构, got: {reason}")
        assert "失效警示" not in reason, "无警示行时不得追加警示面"

    def test_b3_tri_state_not_changed_by_warnings(self, mem_db):
        """警示不改三态: 有警示时 buy_now 仍 buy_now (警示非否决, 二段定谳)。"""
        conn = mem_db.get_connection()
        conn.execute(
            "UPDATE stock_pool SET last_price=8.13 WHERE code='600152'")
        conn.execute(
            "INSERT INTO agent_invalidation_warnings "
            "(code, date, expert_name, condition) VALUES "
            "('600152', '2026-09-15', 'tech', '跌破 9.5 支撑线')")
        conn.commit()
        from execution_layer.scripts.ex4_cli_scan import advisor_segment
        advisor_segment(mem_db, today="2026-09-15", now_hhmm="15:00")
        rows = conn.execute(
            "SELECT reason FROM action_records WHERE action_type='advisory_state' "
            "AND code='600152'").fetchall()
        assert rows and rows[0][0].startswith("buy_now:"), (
            "RED: 警示是附加面非否决面 — 价内 buy_now 判定不得被警示改态")
