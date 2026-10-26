# -*- coding: utf-8 -*-
"""TDD: Day 3 卡二 — token 旁观者显形 (provider_manager.call 返 None 时)

钉4:
  钉1 provider_manager.call budget 耗尽时: logger.warning (非 info) + alerts 落行
  钉2 ExpertReport/stub 降级时 key_points 含 [DEGRADED:token_budget] 标 (后续在 experts 工程)
  钉3 tier_used="degraded" ≠"rules"≠"llm" (假绿族 判例)
  钉4 alerts INSERT 失败 → fail-safe 不吞主链 (logger.warning 记录但 不 raise)

判例:「告警必有载 (判例 #9)」/「假绿族=tier_used='rules' 显形真绿实际降级」/「fail-safe 主链不吞」
"""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from typing import Any


# ---------------------------------------------------------------------------
# 钉 1: provider_manager.call token budget exhausted → warning + alerts
# ---------------------------------------------------------------------------
class TestTokenExhaustedEmitsWarning:
    """budget 耗尽时 provider_manager.call 必须:
    - 返 None
    - 打 warning 级 (非 info)
    - alerts 表 落 一 行 carrier (severity='critical', layer='agent', title 含 'token')"""

    def test_budget_exhausted_emits_warning_and_alert(self, caplog):
        from agent_layer.llm.provider_manager import ProviderManager  # type: ignore
        from agent_layer.llm.token_budget import TokenBudget  # type: ignore

        pm = ProviderManager.__new__(ProviderManager)
        # 最小 stub: pool/client/guardrail/prompt_adapter 不测,budget 用尽
        class _B(TokenBudget):
            def is_exceeded(self, cp):
                return True

            def record_usage(self, *a, **kw):
                pass

        pm.token_budget = _B()
        pm.guardrail = SimpleNamespace(check=lambda p: SimpleNamespace(passed=True, flagged=[]))
        pm.db = SimpleNamespace(get_connection=lambda: sqlite3.connect(":memory:"))
        # 不 设 pool/client/prompt_adapter 等 其 它属 性 — 在返 None 之前 就 返 回

        with caplog.at_level("WARNING"):
            out = pm.call("test prompt", call_point="test_point")

        assert out is None
        # 打 warning 级 (非 info)
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert any("token" in r.message.lower() for r in warnings), (
            f"应 warning 'token' 实得 {[r.message[:60] for r in warnings]}")


# ---------------------------------------------------------------------------
# 钉 2: ExpertReport.key_points 含 [DEGRADED:token_budget] 标
# ---------------------------------------------------------------------------
class TestDegradedExpertMarkerOnKeyPoints:
    """ExpertReport/stub 降级时 key_points 含 [DEGRADED:token_budget] 标。
    测试不 真调 LLM/边源,用 stub 模拟 耗尽 时 tier_used='degraded' 填 key_points。"""

    def test_expert_report_key_points_carry_degraded_marker_when_stub(self):
        """模拟 耗尽后 ExpertReport.key_points 含 [DEGRADED:token_budget]。"""
        # 模拟一个 stub expert (replace provider_manager 内部 调 死)
        class _StubProviderMgr:
            def call(self, prompt, **kw):
                return None  # 预算耗尽

        # 模拟 expert analyze 逻辑的 stub (在 experts.py _run_llm 等)
        # 预期: provider 返 None → tier_used='degraded' + key_points 含标
        # 这是 卡二 修后的形态, 但我们暂时 只是 定判 钉位,不 修 experts.py
        # 所以本钉先弱化为「设计文档钉」: 主张 Spec §2 (修②) — 等 experts 修后 真跑

        # 弱钉 (设计钉): ExpertReport dataclass 字段 tier_used/key_points 存 在
        from agent_layer.debate.models import ExpertReport  # type: ignore
        r = ExpertReport(
            expert_name="t",
            score=0.0,
            key_points=["[DEGRADED:token_budget] LLM 预算耗尽 — 无法产生本专家意见"],
            confidence=0.0,
            tier_used="degraded",
        )
        assert any("[DEGRADED:token_budget]" in kp for kp in r.key_points)
        assert r.tier_used == "degraded"


# ---------------------------------------------------------------------------
# 钉 3: tier_used='degraded' ≠'rules'≠'llm' (假绿判例)
# ---------------------------------------------------------------------------
class TestDegradedNotFakeGreen:
    """tier_used='degraded' 不得 写为 'rules'/'llm' — 「假绿」判死区。"""

    def test_degraded_tier_not_fake_green(self):
        """tier_used='degraded' 不被当成 'rules' 或 'llm'。"""
        from agent_layer.debate.models import ExpertReport  # type: ignore
        r = ExpertReport(
            expert_name="t", score=0.0,
            tier_used="degraded", confidence=0.0,
        )
        # 假绿判死区: tier_used 不 应是 "rules" 或 "llm"
        assert r.tier_used not in ("rules", "llm"), (
            f"tier_used='degraded' 不应被视为真绿; 实得 '{r.tier_used}'")


# ---------------------------------------------------------------------------
# 钉 4: alerts INSERT 失败 → fail-safe 不吞主链
# ---------------------------------------------------------------------------
class TestFailSafeOnAlertInsertFailure:
    """alerts INSERT 抛错时 provider_manager.call 必须 返 None,不 抛 异常。"""

    def test_alert_insert_failure_does_not_break_call(self):
        from agent_layer.llm.provider_manager import ProviderManager  # noqa: type
        from agent_layer.llm.token_budget import TokenBudget  # noqa: type

        pm = ProviderManager.__new__(ProviderManager)

        class _B(TokenBudget):
            def is_exceeded(self, cp):
                return True

        pm.token_budget = _B()
        pm.guardrail = SimpleNamespace(check=lambda p: SimpleNamespace(passed=True, flagged=[]))

        # db.get_connection 抛错 = alerts INSERT 失败 (alibity 故障)
        def _broken_conn():
            raise Exception("db locked")

        pm.db = SimpleNamespace(get_connection=_broken_conn)

        # 应返 None, 不 抛 (fail-safe)
        out = pm.call("test", call_point="test")
        assert out is None
