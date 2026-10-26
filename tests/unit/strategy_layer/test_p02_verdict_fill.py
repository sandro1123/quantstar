# -*- coding: utf-8 -*-
"""TDD: P0-2 verdict dataclass 补字段 + deep_review 真填值 (派单 sm-2d7d72e0e5244dfa Day 2)

三钉覆盖:
  钉1 AgentReviewResult 含 verdict 字段 + LLM 腿填 response.verdict
  钉2 _rule_based_review 依 confidence 推 verdict (推荐 / 谨慎推荐 / 观望 / 不推荐)
  钉3 _default_result verdict='观望' + deep_top_n PASS 闸 验收 (verdict 出现在 gate result)

判例:
  - dataclass 锚须钉「真填值」非「getattr None or ''」 — 第十变体判例
  - _rule_based_review verdict 推断只看 confidence, 4 段桶 (深推荐分界)
  - _default 是一种诚实降级 (agent layer 完全坏时) → verdict='观望' 不谎称推荐
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any


# ---------------------------------------------------------------------------
# 钉 1: AgentReviewResult dataclass 必含 verdict 字段
# ---------------------------------------------------------------------------
class TestAgentReviewResultVerdictField:
    """dataclass 契约级钉: verdict 字段必须存在. LLM 路径必须填真值。"""

    def test_dataclass_has_verdict_field(self):
        from strategy_layer.agent_gate.deep_review import (  # type: ignore
            AgentReviewResult)
        import dataclasses
        names = {f.name for f in dataclasses.fields(AgentReviewResult)}
        assert "verdict" in names, (
            f"AgentReviewResult 缺 verdict 字段 — 第十变体判例 = dataclass 缺字 "
            f"段; 现字段 {sorted(names)}")

    def test_llm_path_returns_response_verdict(self):
        """review_candidate (正常非深) 必返 response.verdict (非空)。"""
        from strategy_layer.agent_gate.deep_review import (  # type: ignore
            AgentReviewResult, DeepReview)

        # Stub AgentLayer: responseverdict='推荐' confidence=0.85
        class _Resp:
            confidence = 0.85
            reasoning = "动量强"
            risk_flags = []
            verdict = "推荐"

        class _AL:
            def analyze(self, request):
                return _Resp()

        dr = DeepReview(agent_layer=_AL())
        out = dr.review_candidate("600000", "2026-10-04", factor_scores={}, deep=False)
        assert out.verdict == "推荐", (
            f"LLM 路径应填 response.verdict='推荐'; 实得 verdict='{out.verdict}'")
        assert out.confidence == 0.85


# ---------------------------------------------------------------------------
# 钉 2: _rule_based_review 依 confidence 推 verdict
# ---------------------------------------------------------------------------
class TestRuleBasedVerdictMapping:
    """_rule_based_review 调时 verdict 必从 confidence 推断 (4 段桶)。"""

    def _dr_with_experts(self, confidence_val: float):
        """构造 DeepReview + stub experts yield confidence=target."""
        from strategy_layer.agent_gate.deep_review import DeepReview  # noqa: type

        class _Rep:
            def __init__(self, c):
                self.confidence = c
                self.score = 0.5 * (c * 2 - 1)  # 让 score 与 confidence 一致
                self.expert_name = "stub"
                self.key_points = ["p1"]

        class _Exp:
            def analyze(self, data):
                return _Rep(confidence_val)

        # 直接 monkeypatch ALL_EXPERTS = [_Exp] 以避免深层依赖
        import agent_layer.debate.experts as experts_mod
        orig = experts_mod.ALL_EXPERTS
        experts_mod.ALL_EXPERTS = [_Exp]
        dr = DeepReview(experts=[_Exp])
        try:
            result = dr._rule_based_review("600000", "2026-10-04",
                                           factor_scores={"momentum_20d": 0.5})
        finally:
            experts_mod.ALL_EXPERTS = orig
        return result

    def test_rule_based_high_confidence_maps_to_recommend(self):
        """confidence >= 0.7 → '推荐'."""
        out = self._dr_with_experts(0.85)
        assert out.verdict == "推荐", (
            f"confidence={out.confidence} 应映 verdict='推荐'; 实得 '{out.verdict}'")

    def test_rule_based_mid_confidence_maps_to_watch(self):
        """0.4 <= confidence < 0.7 → '观望'."""
        out = self._dr_with_experts(0.55)
        assert out.verdict == "观望", (
            f"confidence={out.confidence} 应映 verdict='观望'; 实得 '{out.verdict}'")

    def test_rule_based_low_confidence_maps_to_reject(self):
        """confidence < 0.4 → '不推荐'."""
        out = self._dr_with_experts(0.25)
        assert out.verdict == "不推荐", (
            f"confidence={out.confidence} 应映 verdict='不推荐'; 实得 '{out.verdict}'")


# ---------------------------------------------------------------------------
# 钉 3: _default_result verdict='观望' (诚实降级)
# ---------------------------------------------------------------------------
class TestDefaultResultVerdictWatch:
    """_default_result (异常路径) verdict='观望' 不谎称推荐."""

    def test_default_result_verdict_is_watch(self):
        from strategy_layer.agent_gate.deep_review import DeepReview  # noqa: type
        dr = DeepReview(agent_layer=None)
        out = dr._default_result("600000", "2026-10-04")
        assert out.verdict == "观望", (
            f"默认降级 verdict 应为 '观望' (诚实不谎称推荐); 实得 '{out.verdict}'")
        assert out.confidence == 0.0, "默认降级 confidence 应为 0"
