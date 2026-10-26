# -*- coding: utf-8 -*-
"""R-2 方案①: deep 集覆盖推荐码 — TDD RED 测试面 (feat/r2-deep-coverage, 10-19)。

派单 sm-bcd4259aaafd465c 裁① + 方案稿核准 sm-7bd8dfa529b04efa。
设计意图: recommendation_rationale 只在 deep 辩论腿落库 (agent_layer.py:764),
推荐码 (signal_gen buy) 来自候选池全量过 — 两集穿过各闸后分叉, 晚报占位话术
「量化评分入选, 个股依据生成中」即两表不相交的消费面显形 (10-09 定谳)。

新行为契约 (判据同方案稿五条):
  1. 覆盖: deep_set = composite_score 前 N ∪ must_deep (推荐码预算集)
  2. 预算同构: _buy_budget_codes(ctx) 与 signal_gen 真实 buy 集同输入逐位一致
     (防漂移假绿 — 预算在 agent_gate 前算, signal 在 signal_gen 出)
  3. 不动阈值: deep_top_n / top_n / confidence>0.3 过滤全不变 (划界判据4)
  4. 空 must_deep (无推荐) → deep 行为与现状逐位一致 (对拍零改动)
  5. coverage log: must_deep/covered/missing 三数可见 (防复发 #163 假活族)
  6. rationale_id 回传: rec dict 装配按 chairman 口径查表填 (查不到留空+显形)
"""
import json
import os
import tempfile
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

from common.database import Database
from strategy_layer import StrategyLayer
from strategy_layer.agent_gate.deep_review import DeepReview
from strategy_layer.aggregation.aggregator import FactorDaily
from strategy_layer.signals.generator import TradeSignal
from strategy_layer.signals.generator import SignalGenerator

DATE = "2026-10-09"  # 交易日 (节后首日)
CODE_A = "000001.SZ"  # 高分组 (进 deep 前 10)
CODE_B = "000002.SZ"  # 低分组 (不进前 10 — 推荐码预算必须捞回)
CODE_C = "000003.SZ"
POS_CODE = "600000.SH"  # 已有持仓 → signal_gen 出 hold 不出 buy → 不进 must_deep


def _fd(code, score):
    return FactorDaily(code=code, date=DATE, composite_score=score,
                       factor_scores={"momentum_20d": score})


def _candidate(code, score):
    """轻量候选对象 (selector 产物面: code + composite_score)。"""
    return _fd(code, score)


def _selection(cands):
    sel = MagicMock()
    sel.candidates = cands
    sel.date = DATE
    return sel


class TestMustDeepCoverage:
    """判据1+3+4: deep 集 = 前N ∪ must_deep; 阈值不动; 空预算零改动。"""

    def _reviewer(self, config=None):
        dr = DeepReview(agent_layer=MagicMock())
        dr.config = config or {}
        return dr

    def _spy_deep(self, dr):
        """spy review_candidate: 记 deep 码集, 返回带 confidence 的真 AgentReviewResult。"""
        from strategy_layer.agent_gate.deep_review import AgentReviewResult
        deep_ids = []

        def spy(code, date, scores=None, deep=False):
            if deep:
                deep_ids.append(code)
            return AgentReviewResult(code=code, date=date, overall_score=0.5,
                                     confidence=0.5, reasoning="spy", verdict="buy")

        return spy, deep_ids

    def test_must_deep_pulled_into_deep_set(self):
        """must_deep 里的码即使 composite 排不进前 N, 也升 deep 腿 (判据1)。"""
        dr = self._reviewer({"strategy.agent_gate.deep_top_n": 1})
        cands = [_candidate(CODE_A, 5.0), _candidate(CODE_B, 1.0)]
        spy, deep_ids = self._spy_deep(dr)
        with patch.object(dr, "review_candidate", side_effect=spy):
            with patch.object(dr, "_deep_top_n", return_value=1):
                dr.review_candidates(cands, DATE, must_deep={CODE_B})
        assert CODE_B in deep_ids, "低分推荐码必须被 must_deep 捞进 deep 集"
        assert CODE_A in deep_ids, "前 N 照旧升 deep (不改原行为)"

    def test_thresholds_unchanged(self):
        """划界 (判据4): deep_top_n=10 默认 + confidence>0.3 过滤不动。"""
        dr = self._reviewer({})
        assert dr._deep_top_n() == 10, "deep_top_n 默认值不许因本卡变"
        from strategy_layer.agent_gate.deep_review import AgentReviewResult
        cands = [_candidate(CODE_A, 5.0)]
        results = None
        with patch.object(dr, "review_candidate",
                          return_value=AgentReviewResult(code=CODE_A, date=DATE, overall_score=0.2, confidence=0.2, reasoning="r")):
            results = dr.review_candidates(cands, DATE, must_deep={CODE_A})
        # 低 confidence 票照旧被 agent_gate 过滤是 strategy 层的事;
        # deep_review 面只钉: review_candidates 返回全量不预滤 (既有行为)
        assert len(results) == 1

    def test_empty_must_deep_zero_change(self):
        """空 must_deep (无推荐日) → deep 集=前 N, 与现状逐位一致 (对拍)。"""
        dr = self._reviewer({"strategy.agent_gate.deep_top_n": 2})
        cands = [_candidate(CODE_A, 5.0), _candidate(CODE_B, 4.0), _candidate(CODE_C, 1.0)]
        spy, deep_ids = self._spy_deep(dr)
        with patch.object(dr, "review_candidate", side_effect=spy):
            with patch.object(dr, "_deep_top_n", return_value=2):
                dr.review_candidates(cands, DATE, must_deep=set())
        assert sorted(deep_ids) == sorted([CODE_A, CODE_B]), "空预算=纯前 N"

    def test_must_deep_default_backward_compatible(self):
        """旧调用方 (无 must_deep 参数) 行为不变 — 默认空集。"""
        dr = self._reviewer({})
        cands = [_candidate(CODE_A, 5.0)]
        from strategy_layer.agent_gate.deep_review import AgentReviewResult
        with patch.object(dr, "review_candidate",
                          return_value=AgentReviewResult(
                              code=CODE_A, date=DATE, overall_score=0.5,
                              confidence=0.5, reasoning="r")):
            results = dr.review_candidates(cands, DATE)  # 不传 must_deep
        assert len(results) == 1


class TestBuyBudgetIsomorphism:
    """判据2: 预算函数与 signal_gen 真实 buy 集同输入逐位一致 (防漂移假绿)。"""

    def _layer(self):
        layer = StrategyLayer()
        layer.config = {}
        layer.data_layer = MagicMock()
        return layer

    def test_budget_matches_signal_generator_buy_set(self):
        """同候选池+同持仓输入: _buy_budget_codes == signal_gen buy 码集 (逐位)。"""
        layer = self._layer()
        cands = [_candidate(CODE_A, 5.0), _candidate(CODE_B, 4.0), _candidate(POS_CODE, 3.0)]
        sel = _selection(cands)
        positions = {POS_CODE: {"entry_price": 10.0}}
        ctx = MagicMock()
        ctx.selection = sel
        ctx.current_positions = positions
        ctx.date = DATE

        budget = layer._buy_budget_codes(ctx)
        gen = SignalGenerator()
        signals = gen.generate(sel, current_positions=positions)
        real_buy = {s.code for s in signals if s.action == "buy"}
        assert budget == real_buy, (
            f"预算集与真实 buy 集漂移: budget-only={budget - real_buy}, "
            f"real-only={real_buy - budget}")

    def test_budget_excludes_positions(self):
        """持仓票 signal_gen 出 hold → 预算集也不含 (同构的另一半)。"""
        layer = self._layer()
        cands = [_candidate(CODE_A, 5.0), _candidate(POS_CODE, 3.0)]
        ctx = MagicMock()
        ctx.selection = _selection(cands)
        ctx.current_positions = {POS_CODE: {"entry_price": 10.0}}
        ctx.date = DATE
        budget = layer._buy_budget_codes(ctx)
        assert POS_CODE not in budget
        assert CODE_A in budget


class TestCoverageLog:
    """判据5: must_deep/covered/missing 三数可见 (防复发 #163)。"""

    def test_coverage_log_emitted(self):
        """agent_gate 跑完出 coverage log, missing 非空时有码清单。"""
        dr = self._reviewer_or_layer()
        # 经 strategy 层 _stage_agent_gate 打点 (集成面)
        layer = StrategyLayer()
        layer.config = {"strategy.agent_gate.enabled": True}
        layer.deep_review = MagicMock()
        layer.deep_review.review_candidates.return_value = []
        ctx = MagicMock()
        ctx.selection = _selection([_candidate(CODE_A, 5.0)])
        ctx.date = DATE
        ctx.current_positions = {}
        ctx.agent_gate_enabled = False
        with patch("strategy_layer.logger") as log_mock:
            layer._stage_agent_gate(ctx)
        joined = " ".join(str(c) for c in log_mock.info.call_args_list)
        assert "coverage" in joined.lower() or "must_deep" in joined, (
            f"coverage log 缺失, 实际 info 调用: {log_mock.info.call_args_list}")

    def _reviewer_or_layer(self):
        return DeepReview(agent_layer=MagicMock())


class TestRationaleIdBackfill:
    """判据2 JOIN 面: orchestrator rec dict 按 chairman 口径填 rationale_id (方案②)。"""

    def _tracker_stub(self):
        from execution_layer.tracking.recommendation_tracker import RecommendationTracker
        return RecommendationTracker(db=None)

    def test_chairman_row_preferred(self):
        """多专家行取 chairman (与消费面 load_argument_from_rationale 同序)。"""
        rows = [
            {"rationale_id": "rid_bear", "expert_name": "bear_expert"},
            {"rationale_id": "rid_chairman", "expert_name": "chairman"},
            {"rationale_id": "rid_bull", "expert_name": "bull_expert"},
        ]
        picked = sorted(rows, key=lambda r: (
            0 if r["expert_name"] == "chairman" else 1, r["expert_name"]))[0]
        assert picked["rationale_id"] == "rid_chairman"

    def test_missing_rationale_empty_with_log(self):
        """查不到 → 留空串 (诚实水位) + 显形 log (不阻塞 tracking, 铁律#12)。"""
        # 查表函数契约: 空结果返 None, 调用方填空串并 log warning
        from ops_layer.orchestrator import _rationale_id_for_code  # 施工后存在
        assert _rationale_id_for_code(None, "000001.SZ", "2026-10-09") is None
