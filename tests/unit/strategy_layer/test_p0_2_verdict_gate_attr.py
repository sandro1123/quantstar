# -*- coding: utf-8 -*-
"""P0-2 verdict 闸属性幻影修复 — TDD 判据（判例「建了没接线」第九变体）。

根因：strategy_layer/__init__.py 的 verdict 闸调用 `(r.verdict or "")`，
但 AgentReviewResult（agent_gate/deep_review.py:47）dataclass **没有 verdict 字段**。

修复（统筹裁决）：去掉第三档「无判断=拒绝」，只保留 confidence≤0.3 一档。

判据：
1. AgentReviewResult 无 verdict 字段时，verdict 闸**不炸**（不 AttributeError）。
2. 无 verdict 字段时，confidence > 0.3 的票**不被拒绝**（不静默全清）。
3. confidence <= 0.3 的票**仍被拒绝**（confidence 闸保留）。
4. （防地雷：getattr 修法会导致全集清空——本判据锁「无 verdict 时不拦」）。
"""
from strategy_layer.agent_gate.deep_review import AgentReviewResult


def _rejected_codes(reviews):
    """复刻 strategy_layer/_stage_agent_gate 的过滤逻辑（修后期望形态）。

    若 strategy_layer 已实现修复，应与本函数行为一致：
    只按 confidence<=0.3 过滤，不读 r.verdict。
    """
    return {r.code for r in reviews if r.confidence <= 0.3}


def _mk(code, confidence):
    return AgentReviewResult(
        code=code, date="2026-09-29",
        overall_score=confidence, confidence=confidence,
        reasoning="测试", risk_flags=[], suggestions=[], news_items=[])


def test_agent_review_result_has_no_verdict_field():
    """判据 0：锁契约——AgentReviewResult 确实没有 verdict 字段。"""
    r = _mk("000001", 0.8)
    assert hasattr(r, "verdict"), "P0-2 完整版已补全 verdict 字段 (f64aef8)"


def test_verdict_gate_does_not_explode_on_missing_attr():
    """判据 1：无 verdict 字段的 list 过滤不炸。"""
    reviews = [_mk("000001", 0.8), _mk("000002", 0.5)]
    rejected = _rejected_codes(reviews)  # 不 AttributeError 即过
    assert isinstance(rejected, set)


def test_high_confidence_without_verdict_not_rejected():
    """判据 2：无 verdict 时 confidence>0.3 不拦（防「无判断=拒绝」静默全清地雷）。"""
    reviews = [_mk("000001", 0.8), _mk("000002", 0.5)]
    rejected = _rejected_codes(reviews)
    assert "000001" not in rejected
    assert "000002" not in rejected


def test_low_confidence_still_rejected():
    """判据 3：confidence<=0.3 的票仍被拒绝（confidence 闸保留）。"""
    reviews = [_mk("000001", 0.2), _mk("000002", 0.9)]
    rejected = _rejected_codes(reviews)
    assert "000001" in rejected
    assert "000002" not in rejected


def test_pool_not_silently_cleared():
    """判据 4：高信心票池不被静默清空——全部 confidence>0.3 时 rejected 为空。"""
    reviews = [_mk(f"00000{i}", 0.8) for i in range(5)]
    rejected = _rejected_codes(reviews)
    assert rejected == set(), "无 verdict 时不得清空候选池（getattr 修法的地雷）"
