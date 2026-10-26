"""AgentGate 审核接口单元测试"""
import pytest
from unittest.mock import MagicMock, patch
from strategy_layer.agent_gate.deep_review import DeepReview, AgentReviewResult
from strategy_layer.aggregation.aggregator import FactorDaily


class TestDeepReview:

    def test_review_candidate_constructs_proper_request(self):
        mock_agent = MagicMock()
        mock_agent.analyze.return_value = MagicMock(
            confidence=0.85,
            reasoning="优质标的，趋势良好",
            risk_flags=[],
        )
        reviewer = DeepReview(agent_layer=mock_agent)
        result = reviewer.review_candidate("000001", "2026-07-17", {"momentum": 0.8, "value": 0.6})

        assert result.code == "000001"
        assert result.date == "2026-07-17"
        assert result.confidence == 0.85
        assert result.reasoning == "优质标的，趋势良好"

        from agent_layer.core.models import AgentRequest
        mock_agent.analyze.assert_called_once()
        request = mock_agent.analyze.call_args[0][0]
        assert isinstance(request, AgentRequest)
        assert request.task == "stock_analysis"
        assert request.input["code"] == "000001"
        assert request.input["factor_scores"] == {"momentum": 0.8, "value": 0.6}
        assert request.depth == "normal"

    def test_review_candidate_no_agent_falls_back_to_rule_based(self):
        reviewer = DeepReview(agent_layer=None)
        result = reviewer.review_candidate("000001", "2026-07-17", {"momentum_20d": 10, "turnover_ratio": 8})
        assert result.confidence > 0.0
        assert "规则式降级分析" in result.reasoning

    def test_review_candidate_agent_raises_exception_falls_back_to_rule_based(self):
        mock_agent = MagicMock()
        mock_agent.analyze.side_effect = ValueError("API timeout")
        reviewer = DeepReview(agent_layer=mock_agent)
        result = reviewer.review_candidate("000001", "2026-07-17", {"momentum_20d": 10, "turnover_ratio": 8})
        assert result.confidence > 0.0
        assert "规则式降级分析" in result.reasoning

    def test_review_candidate_both_fail_returns_default(self):
        reviewer = DeepReview(agent_layer=None)
        with patch.object(reviewer, '_rule_based_review', side_effect=RuntimeError("expert crash")):
            result = reviewer.review_candidate("000001", "2026-07-17")
            assert result.confidence == 0.0
            assert result.reasoning == "AgentLayer not available"

    def test_rule_based_review_produces_reasonable_score(self):
        reviewer = DeepReview(agent_layer=None)
        result = reviewer.review_candidate("000001", "2026-07-17", {
            "momentum_20d": 15, "turnover_ratio": 12, "pe": 25, "volume_change": 30, "market_cap": 1e9,
        })
        assert 0 <= result.confidence <= 1
        assert result.overall_score == result.confidence
        assert "规则式降级分析" in result.reasoning
        assert len(result.suggestions) > 0

    def test_review_candidate_agent_returns_risk_flags(self):
        from agent_layer.core.models import RiskFlag
        mock_agent = MagicMock()
        mock_agent.analyze.return_value = MagicMock(
            confidence=0.4,
            reasoning="波动率过高",
            risk_flags=[RiskFlag(flag="高波动", severity="high"), RiskFlag(flag="低流动性", severity="medium")],
        )
        reviewer = DeepReview(agent_layer=mock_agent)
        result = reviewer.review_candidate("000001", "2026-07-17")
        assert "高波动" in result.risk_flags
        assert "低流动性" in result.risk_flags

    def test_review_candidate_empty_factor_scores_rule_based(self):
        reviewer = DeepReview(agent_layer=None)
        result = reviewer.review_candidate("000001", "2026-07-17")
        assert 0 <= result.confidence <= 1
        assert "规则式降级分析" in result.reasoning

    def test_review_candidates_filters_low_confidence(self):
        mock_agent = MagicMock()
        def side_effect(req):
            confidence = 0.8 if req.input["code"] == "000001" else 0.2
            return MagicMock(confidence=confidence, reasoning="ok", risk_flags=[])
        mock_agent.analyze.side_effect = side_effect

        reviewer = DeepReview(agent_layer=mock_agent)
        candidates = [
            FactorDaily(code="000001", date="2026-07-17", composite_score=1.5, rank=1),
            FactorDaily(code="000002", date="2026-07-17", composite_score=0.5, rank=2),
        ]
        results = reviewer.review_candidates(candidates, "2026-07-17")

        assert len(results) == 2
        passed = [r for r in results if r.confidence > 0.3]
        assert len(passed) == 1
        assert passed[0].code == "000001"

    def test_agent_rejection_filtering_in_pipeline(self):
        from strategy_layer.selection.selector import SelectionResult
        from strategy_layer.signals.generator import SignalGenerator

        mock_agent = MagicMock()
        def side_effect(req):
            confidence = 0.8 if req.input["code"] == "000001" else 0.1
            return MagicMock(confidence=confidence, reasoning="ok", risk_flags=[])
        mock_agent.analyze.side_effect = side_effect

        reviewer = DeepReview(agent_layer=mock_agent)
        candidates = [
            FactorDaily(code="000001", date="2026-07-17", composite_score=1.5, rank=1),
            FactorDaily(code="000002", date="2026-07-17", composite_score=0.5, rank=2),
        ]
        selection = SelectionResult(date="2026-07-17", candidates=candidates)

        agent_reviews = reviewer.review_candidates(selection.candidates, "2026-07-17")
        rejected_codes = {r.code for r in agent_reviews if r.confidence <= 0.3}
        selection.candidates = [c for c in selection.candidates if c.code not in rejected_codes]

        assert len(selection.candidates) == 1
        assert selection.candidates[0].code == "000001"

        gen = SignalGenerator()
        signals = gen.generate(selection)
        assert len(signals) == 1
        assert signals[0].code == "000001"