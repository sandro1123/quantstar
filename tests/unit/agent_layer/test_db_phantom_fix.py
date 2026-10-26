# -*- coding: utf-8 -*-
"""断点修复 TDD — AgentLayer db 属性幻影（统筹批 09-26，半天卡）

■ 根因（三方合力定谳）：设计稿发现「没通电」→ W1 找到「错线在哪」（583/690
  用 getattr(self,"db",None)）→ 6f73b79e 定位到「插头型号不对」（__init__ 存
  self._database，self.db 属性从未存在）→ persist 首行静默 return，死 9 天，
  不抛异常无 warning 零痕迹。

■ 修法（统筹批）：AgentLayer 加 `db` property（单一定义点，优于三处改名——
  改三处将来还有人写 self.db 再踩）。

■ 三钉（统筹批 + 端到端锚）：
  ① 构造后 self.db 非 None（防幻影复发）
  ② _run_debate 跑过 → recommendation_rationale 表必有行（端到端锚，#7 卡验收）
  ③ _persist_invalidation_warnings 同测（同挂点同修 + 622 空集静默第二病）
"""
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))


@pytest.fixture()
def al():
    """最小 AgentLayer（跳过重依赖装配，只测 db 属性与 persist 链）"""
    from agent_layer.core.agent_layer import AgentLayer
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    layer = AgentLayer.__new__(AgentLayer)   # 跳过 __init__ 重依赖
    layer._database = SimpleNamespace(get_connection=lambda: conn)
    layer._pm = SimpleNamespace(has_available_llm=lambda: False)
    yield layer, conn
    conn.close()


class TestNail1_DbPropertyExists:
    """钉①：构造后 self.db 非 None —— 幻影防复发锚"""

    def test_db_property_returns_database(self, al):
        layer, conn = al
        assert layer.db is not None, \
            "self.db 幻影复发（__init__ 存 _database 而消费方读 self.db）"

    def test_db_property_returns_same_object(self, al):
        """property 必须返回同一个连接源（单一定义点语义）"""
        layer, _ = al
        assert layer.db.get_connection() is layer._database.get_connection()

    def test_db_none_when_no_database(self):
        """未传库（裸构造）→ db 为 None（合法降级，非异常）"""
        from agent_layer.core.agent_layer import AgentLayer
        layer = AgentLayer.__new__(AgentLayer)
        layer._database = None
        assert layer.db is None


class TestNail2_RationaleEndToEnd:
    """钉②：persist 走通 → rationale 表必有行（端到端锚）"""

    def test_persist_writes_row(self, al):
        layer, conn = al
        from agent_layer.core.agent_layer import AgentLayer
        conn.execute(
            "CREATE TABLE IF NOT EXISTS recommendation_rationale ("
            "rationale_id TEXT PRIMARY KEY, code TEXT NOT NULL, rec_date TEXT NOT NULL,"
            "verdict TEXT, confidence REAL, position_pct REAL, reasoning TEXT,"
            "expert_name TEXT, key_points TEXT, invalidation_conditions TEXT,"
            "claims TEXT, trace_id TEXT, run_id TEXT, request_id TEXT,"
            "advisor_text TEXT, advisor_guard TEXT,"
            "created_at TEXT DEFAULT (datetime('now','+8 hours')),"
            "UNIQUE (code, rec_date, expert_name))")
        conn.commit()
        final = SimpleNamespace(verdict="推荐", confidence=0.72, position_pct=0.15,
                                reasoning="动量因子近20日强势", rationale_id="")
        expert = SimpleNamespace(expert_name="chairman", key_points=["动量强"],
                                 invalidation_conditions=["跌破10.5元"])
        AgentLayer._persist_recommendation_rationale(layer, final, [expert],
                                                     "600000", "2026-09-26")
        n = conn.execute("SELECT COUNT(*) FROM recommendation_rationale").fetchone()[0]
        assert n == 1, f"persist 走通后 rationale 表必有行，实得 {n}"

    def test_persist_skips_only_when_no_db(self):
        """db=None（裸构造）→ 合法静默跳过（这是唯一的合法静默路径）"""
        from agent_layer.core.agent_layer import AgentLayer
        layer = AgentLayer.__new__(AgentLayer)
        layer._database = None
        final = SimpleNamespace(verdict="推荐", confidence=0.7, reasoning="x",
                                position_pct=0.1)
        expert = SimpleNamespace(expert_name="t", key_points=[], invalidation_conditions=[])
        # 不抛即合规
        AgentLayer._persist_recommendation_rationale(layer, final, [expert],
                                                     "600000", "2026-09-26")


class TestNail3_InvalidationSameFix:
    """钉③：invalidation 同挂点同修 + 622 空集静默第二病"""

    def test_invalidation_writes_rows(self, al):
        layer, conn = al
        from agent_layer.core.agent_layer import AgentLayer
        # 权威 DDL（execution_layer/tables.py:226）—— 判例：夹具用真 DDL，
        # 防手抄夹具落后真 schema（手抄少列 = 同款坑第三次）
        from execution_layer.tables import AGENT_INVALIDATION_WARNINGS_DDL as _ddl
        conn.execute(_ddl)
        conn.commit()
        expert = SimpleNamespace(expert_name="tech",
                                 invalidation_conditions=["跌破10.5元止损"])
        n = AgentLayer._persist_invalidation_warnings(layer, [expert], "600000",
                                                      "2026-09-26")
        assert n >= 1, "invalidation 落库走通后应有行"

    def test_invalidation_empty_returns_zero_explicitly(self, al):
        """622 空集：返回 0 且**打日志**（空集显式报警，不许静默）"""
        import logging
        layer, conn = al
        from agent_layer.core.agent_layer import AgentLayer
        with_cap = []
        handler = logging.Handler()
        handler.emit = lambda rec: with_cap.append(rec.getMessage())
        lg = logging.getLogger("agent")   # 真 logger 名（common.logging get_logger("agent")）
        lg.addHandler(handler)
        try:
            n = AgentLayer._persist_invalidation_warnings(
                layer, [SimpleNamespace(expert_name="t", invalidation_conditions=[])],
                "600000", "2026-09-26")
        finally:
            lg.removeHandler(handler)
        assert n == 0
        assert any("invalidation" in m.lower() or "空" in m for m in with_cap), \
            "空集须显式留痕（判例：空集与零命中必须可区分）"
