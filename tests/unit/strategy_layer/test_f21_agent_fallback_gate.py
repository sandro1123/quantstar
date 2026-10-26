"""TDD: F-21 agent gate 系统性降级止血测试.

三钉验收:
1. AgentResponse 携带 degraded_fallback=True (当 _fallback_response 触发).
2. DeepReview 将 degraded_fallback 透传至 AgentReviewResult.
3. strategy_layer _stage_agent_gate 当遇到 degraded 兜底 (confidence<=0.3 但 degraded_fallback=True):
   - 绝不静默清空候选池;
   - 保留候选并打 warning 告警留痕;
   - 真实业务低置信 (degraded_fallback=False 且 confidence<=0.3) 依旧精准拦截.
"""

from unittest.mock import MagicMock
import pytest

from agent_layer.core.agent_layer import AgentLayer
from agent_layer.core.models import AgentRequest, AgentResponse
from strategy_layer import StrategyLayer, StrategyStageContext
from strategy_layer.agent_gate.deep_review import DeepReview, AgentReviewResult
from strategy_layer.aggregation.aggregator import FactorDaily
from strategy_layer.selection.selector import SelectionResult


def test_agent_fallback_response_marks_degraded():
    """钉1: _fallback_response 产生的结果必须显式标记 degraded_fallback=True."""
    al = AgentLayer()
    req = AgentRequest(task="stock_analysis", input={"code": "600000"})
    resp = al._fallback_response(req, "测试超时降级")
    assert resp.degraded_fallback is True
    assert resp.confidence == 0.3
    assert resp.verdict == "观望"


def test_deep_review_propagates_degraded_fallback():
    """钉2: DeepReview review_candidate 遇到 degraded_fallback=True 应透传."""
    mock_agent = MagicMock()
    mock_agent.analyze.return_value = AgentResponse(
        request_id="r1", task="stock_analysis", code="600000",
        verdict="观望", confidence=0.3, reasoning="超时降级「未调LLM」",
        degraded_fallback=True,
    )
    dr = DeepReview(agent_layer=mock_agent)
    out = dr.review_candidate("600000", "2026-10-20", factor_scores={})
    assert out.degraded_fallback is True
    assert out.confidence == 0.3


def test_stage_agent_gate_does_not_clear_candidates_on_system_degradation():
    """钉3: 系统性故障/全线超时降级时，agent_gate 不得将 15/15 候选斩首清空."""
    candidates = [
        FactorDaily(code=f"60000{i}", date="2026-10-20", composite_score=1.0 - i * 0.05, rank=i + 1)
        for i in range(15)
    ]
    ctx = StrategyStageContext(date="2026-10-20")
    ctx.selection = SelectionResult(date="2026-10-20", candidates=list(candidates))

    # 模拟 15 只全部因 LLM 挂掉而返回 degraded 兜底 (confidence=0.3, degraded_fallback=True)
    reviews = [
        AgentReviewResult(
            code=c.code, date="2026-10-20", overall_score=0.3, confidence=0.3,
            reasoning="超时降级「未调LLM」", verdict="观望", degraded_fallback=True,
        )
        for c in candidates
    ]

    mock_dr = MagicMock()
    mock_dr.review_candidates.return_value = reviews

    strat = StrategyLayer()
    strat.deep_review = mock_dr

    strat._stage_agent_gate(ctx)

    # 断言: 候选池绝不归零，15 只完好保留 (或按降级保底放行)
    assert len(ctx.selection.candidates) == 15, (
        f"系统性降级下不得清空候选池, 实得 len={len(ctx.selection.candidates)}")


def test_stage_agent_gate_still_rejects_genuine_low_confidence():
    """钉4 (反向防线): 真实的业务低置信 (degraded_fallback=False, confidence<=0.3) 必须被正常剔除."""
    c1 = FactorDaily(code="600001", date="2026-10-20", composite_score=1.0, rank=1)
    c2 = FactorDaily(code="600002", date="2026-10-20", composite_score=0.8, rank=2)
    ctx = StrategyStageContext(date="2026-10-20")
    ctx.selection = SelectionResult(date="2026-10-20", candidates=[c1, c2])

    reviews = [
        AgentReviewResult(
            code="600001", date="2026-10-20", overall_score=0.8, confidence=0.8,
            reasoning="良好", verdict="推荐", degraded_fallback=False,
        ),
        AgentReviewResult(
            code="600002", date="2026-10-20", overall_score=0.2, confidence=0.2,
            reasoning="基本面恶化", verdict="不推荐", degraded_fallback=False,
        ),
    ]

    mock_dr = MagicMock()
    mock_dr.review_candidates.return_value = reviews

    strat = StrategyLayer()
    strat.deep_review = mock_dr

    strat._stage_agent_gate(ctx)

    # 断言: 真正的低置信 600002 正常剔除，600001 保留
    passed_codes = [c.code for c in ctx.selection.candidates]
    assert passed_codes == ["600001"]
