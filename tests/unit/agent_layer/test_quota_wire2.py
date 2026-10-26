# -*- coding: utf-8 -*-
"""AG-QUOTA 第二段 TDD RED 底稿 — 配额真源根治四件。

依据: 统筹派单 sm-908e7367 + 侦察面判读 sm-ba4ff3a3 (灯绿)。
hermetic: 内存 sqlite + tmp 文件 db, 不碰生产库/不碰 9router 真 db。

RED 语义 (施工前必须红):
- Q1 真源: quota_manager 有 fetch_usage_daily() 真源函数 (sqlite mode=ro 直读
  usageDaily 表, 照 llm.yaml key_source 先例), remaining() 基于真源;
  真源不可达 → 显式 degraded 状态位 (绝不静默沿用虚构 1000)。
- Q2 工厂: agent_layer.build_request() 单一入口 — 12 消费点构造收口,
  温度/格式/默认值统一; 幂等语义 (等价构造)。
- Q3 token 瘦身: 按 agent 类型裁剪 — data_summary 字段包按 task 白名单
  (技术类不吃财务三表); profile_for(task) 返回白名单 dict。
- Q4 防未来护栏: quick 路径 last_price 与现价差 <0.3% 且无新信号 → 快速通过
  不进 LLM; 临线 (距触发线 <2%) 才升 LLM; last_check_price 落库函数。
"""
import os
import sqlite3
import tempfile

import pytest


def _mk_usage_db(tmp_path: str) -> str:
    """仿 9router db 的 usageDaily 表 (最小 schema + 真实 JSON 形态)。"""
    p = os.path.join(tmp_path, "data.sqlite")
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE usageDaily (dateKey TEXT, data TEXT)")
    con.execute(
        "INSERT INTO usageDaily VALUES ('2026-09-10', "
        "'{\"requests\": 1870, \"cost\": 0.0648, "
        "\"byApiKey\": {\"sk-4166fcd0\": {\"requests\": 262, \"cost\": 0.02}}}')")
    con.commit()
    con.close()
    return p


class TestQ1RealSource:
    def test_quota_manager_has_fetch_usage_daily(self):
        from agent_layer.llm.quota_manager import QuotaManager
        assert hasattr(QuotaManager, "fetch_usage_daily"), (
            "真源函数缺: 须 sqlite mode=ro 直读 usageDaily (key_source 先例)")

    def test_remaining_from_real_source(self, tmp_path):
        from agent_layer.llm.quota_manager import QuotaManager
        qm = QuotaManager()
        db = _mk_usage_db(str(tmp_path))
        # 真源接线: budget 500 requests, 已用 262 → remaining 238
        qm.configure(daily_budget_requests=500, source_path=db,
                     api_key_hint="sk-4166fcd0", date_key="2026-09-10")
        assert qm.remaining_requests() == 238, "remaining 须基于真源 not 虚构"

    def test_degraded_explicit_when_source_unreachable(self, tmp_path):
        from agent_layer.llm.quota_manager import QuotaManager
        qm = QuotaManager()
        qm.configure(daily_budget_requests=500,
                     source_path=str(tmp_path) + "/nonexistent.sqlite",
                     api_key_hint="sk-4166fcd0", date_key="2026-09-10")
        # 真源不可达: degraded 显式位 + remaining 不得沿用虚构 1000
        # (degraded 位是惰性探活产物 — 先触发 remaining 探真源再断言位)
        assert qm.remaining_requests() is None, (
            "degraded 态 remaining=None (禁静默沿用虚构数)")
        assert qm.is_degraded() is True, "真源不可达须显式 degraded"
        assert qm.degraded_reason, "degraded 须带原因 (可观测)"

    def test_config_keys_in_ops_yaml(self):
        import yaml
        with open("config/defaults/ops.yaml", encoding="utf-8") as f:
            ops = yaml.safe_load(f)
        quota = ops["ops"].get("agent", {}).get("quota", {})
        assert quota.get("daily_budget_requests"), "ops.yaml 须有 agent.quota 段"
        assert quota.get("low_remaining_pct") is not None


class TestQ2BuildRequestFactory:
    def test_factory_exists_and_defaults(self):
        from agent_layer.core.models import AgentRequest
        import agent_layer
        req = agent_layer.build_request(task="entry_check",
                                        input={"code": "000001"},
                                        consumer="execution")
        assert isinstance(req, AgentRequest)
        assert req.depth == "quick", "execution 类默认 quick 档"
        assert req.format == "structured", "格式统一 structured"

    def test_factory_task_profiles(self):
        import agent_layer
        # deep_review 类任务 → depth=deep 默认
        req = agent_layer.build_request(task="stock_analysis",
                                        input={"code": "000001"},
                                        consumer="strategy")
        assert req.depth in ("deep", "normal"), "strategy deep 面默认深档"

    def test_consumer_sites_use_factory(self):
        """12 外部消费点改走工厂 (抽查 3 个代表文件源码含 build_request)。"""
        import io
        for path in ("src/execution_layer/core/entry_engine.py",
                     "src/report_layer/sections/_llm_helper.py",
                     "src/strategy_layer/agent_gate/deep_review.py"):
            src = io.open(path, encoding="utf-8").read()
            assert "build_request(" in src, f"{path} 须走工厂"
            assert "AgentRequest(" not in src, f"{path} 须收口 (无裸构造)"


class TestQ3TokenProfiles:
    def test_profile_for_exists(self):
        from agent_layer.core.request_profiles import profile_for
        assert hasattr(profile_for, "__call__"), "字段包白名单函数缺"

    def test_technical_task_no_financial_tables(self):
        from agent_layer.core.request_profiles import profile_for
        p = profile_for("market_analysis")
        assert "financial" not in p.get("fields", []), (
            "技术类 agent 不吃财务三表字段包")

    def test_stock_analysis_keeps_core_fields(self):
        from agent_layer.core.request_profiles import profile_for
        p = profile_for("stock_analysis")
        assert "code" in p.get("fields", []) and "date" in p.get("fields", [])


class TestQ4Guardrail:
    def test_guardrail_module_exists(self):
        from agent_layer.core import guardrail as g
        assert hasattr(g, "should_fast_pass"), "快速通过判定函数缺"

    def test_fast_pass_under_threshold(self):
        from agent_layer.core.guardrail import should_fast_pass
        # 现价与上次差 <0.3% 且无新信号 → True (快速通过不进 LLM)
        assert should_fast_pass(last_price=10.0, current_price=10.02,
                                new_signal=False) is True

    def test_no_fast_pass_over_threshold(self):
        from agent_layer.core.guardrail import should_fast_pass
        # 差 >0.3% → False (须真检查)
        assert should_fast_pass(last_price=10.0, current_price=10.05,
                                new_signal=False) is False

    def test_new_signal_never_fast_pass(self):
        from agent_layer.core.guardrail import should_fast_pass
        assert should_fast_pass(last_price=10.0, current_price=10.01,
                                new_signal=True) is False

    def test_near_line_escalates(self):
        from agent_layer.core.guardrail import is_near_line
        # 距触发线 <2% → True (升 LLM 复核)
        assert is_near_line(current_price=9.85, trigger_price=10.0) is True
        assert is_near_line(current_price=9.0, trigger_price=10.0) is False

    def test_last_check_price_persisted(self, tmp_path):
        from agent_layer.core.guardrail import record_last_check
        p = os.path.join(str(tmp_path), "g.sqlite")
        con = sqlite3.connect(p)
        con.execute("CREATE TABLE agent_last_check (code TEXT, task TEXT,"
                    " last_price REAL, checked_at TEXT,"
                    " PRIMARY KEY (code, task))")
        con.commit()
        record_last_check(con, code="000001", task="exit_review",
                          last_price=10.02)
        row = con.execute("SELECT last_price FROM agent_last_check WHERE"
                          " code='000001' AND task='exit_review'").fetchone()
        assert row and abs(row[0] - 10.02) < 1e-9, "last_check_price 须落库"
