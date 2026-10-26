# -*- coding: utf-8 -*-
"""P0-2 TDD — agent_gate verdict 闸（策略池生成时过滤，统筹裁 09-28）

现状：_stage_agent_gate 只过滤 confidence<=0.3（低信心），verdict 是拒绝的票
照样进 selection.candidates → 照样入池。
修法：verdict ∈ {不推荐, 观望, 警惕, 卖出, 规避} 的票同样剔除（两闸 OR 关系）。

判例对齐：闸A（动作一致）——「入池 buy 的票，agent verdict 必须与之相容」，
与 enrich_reason.passes_action_gate 同一判例在策略层的落位。
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from strategy_layer.agent_gate.deep_review import DeepReview


class FakeAgentLayer:
    """返回固定 verdict+confidence 的 review"""

    def __init__(self, reviews):
        self._reviews = reviews

    def analyze(self, req):
        for r in self._reviews:
            if r.code == req.input.get("code"):
                return r
        return None


def _mk_candidate(code, score=0.8):
    return SimpleNamespace(code=code, composite_score=score)


def _mk_review(code, verdict, confidence):
    return SimpleNamespace(
        code=code, verdict=verdict, confidence=confidence,
        reasoning="x", key_points=[], invalidation_conditions=[],
        factors=[], numbers=[])


class TestVerdictGate:
    """P0-2：verdict 闸必须在策略池生成时生效"""

    def _gate(self, reviews, candidates):
        reviewer = DeepReview(agent_layer=FakeAgentLayer(reviews))
        reviewer.review_candidates = lambda cands, date: reviews
        return reviewer, candidates

    def test_reject_verdict_filtered(self):
        """verdict=不推荐 → 被剔除（即使 confidence 高）"""
        cands = [_mk_candidate("600000")]
        reviews = [_mk_review("600000", "不推荐", 0.9)]
        gate, _ = self._gate(reviews, cands)
        # 现状应过滤掉
        kept = [c for c in cands if c.code not in {
            r.code for r in reviews
            if r.confidence <= 0.3 or (r.verdict or "") in {"不推荐","观望","警惕","卖出","规避"}}]
        assert kept == [], "不推荐票应被剔除"

    def test_hold_verdict_filtered(self):
        """verdict=观望 → 被剔除（即使 confidence 高）"""
        cands = [_mk_candidate("600000")]
        reviews = [_mk_review("600000", "观望", 0.9)]
        kept = [c for c in cands if c.code not in {
            r.code for r in reviews
            if r.confidence <= 0.3 or (r.verdict or "") in {"不推荐","观望","警惕","卖出","规避"}}]
        assert kept == []

    def test_buy_verdict_low_confidence_filtered(self):
        """verdict=推荐 + confidence 低 → 被剔除（confidence 闸仍生效）"""
        cands = [_mk_candidate("600000")]
        reviews = [_mk_review("600000", "推荐", 0.2)]
        kept = [c for c in cands if c.code not in {
            r.code for r in reviews
            if r.confidence <= 0.3 or (r.verdict or "") in {"不推荐","观望","警惕","卖出","规避"}}]
        assert kept == []

    def test_buy_verdict_high_confidence_passes(self):
        """verdict=推荐 + confidence 高 → 通过（双闸放行）"""
        cands = [_mk_candidate("600000")]
        reviews = [_mk_review("600000", "推荐", 0.8)]
        kept = [c for c in cands if c.code not in {
            r.code for r in reviews
            if r.confidence <= 0.3 or (r.verdict or "") in {"不推荐","观望","警惕","卖出","规避"}}]
        assert len(kept) == 1

    def test_empty_verdict_rejected(self):
        """verdict 缺失 → 拒（不拿无判断的票入池）"""
        cands = [_mk_candidate("600000")]
        reviews = [_mk_review("600000", "", 0.9)]
        kept = [c for c in cands if c.code not in {
            r.code for r in reviews
            if r.confidence <= 0.3 or not (r.verdict or "").strip() or
               (r.verdict or "") in {"不推荐","观望","警惕","卖出","规避"}}]
        assert kept == []

    def test_mixed_pool_partial_filter(self):
        """混合池：推荐票留，拒绝票剔"""
        cands = [_mk_candidate(c) for c in ("600000", "000001", "300999")]
        reviews = [
            _mk_review("600000", "推荐", 0.8),
            _mk_review("000001", "不推荐", 0.9),
            _mk_review("300999", "观望", 0.7),
        ]
        reject = {r.code for r in reviews
                  if r.confidence <= 0.3 or (r.verdict or "") in {"不推荐","观望","警惕","卖出","规避"}}
        kept = [c.code for c in cands if c.code not in reject]
        assert kept == ["600000"]
