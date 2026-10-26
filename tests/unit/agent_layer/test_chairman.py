"""Chairman + 全部7个分析师 单元测试"""
import pytest
from unittest.mock import MagicMock
from agent_layer.committee.chairman import Chairman, ALL_ANALYSTS
from agent_layer.committee.analyst_value import (
    ValueAnalyst, MomentumAnalyst, RiskAnalyst,
    TechnicalAnalyst, FundamentalAnalyst, SentimentAnalyst, QuantAnalyst,
)


class TestAnalysts:
    @pytest.mark.parametrize("AnalystClass", [
        ValueAnalyst, MomentumAnalyst, RiskAnalyst,
        TechnicalAnalyst, FundamentalAnalyst, SentimentAnalyst, QuantAnalyst,
    ])
    def test_analyst_returns_valid_structure(self, AnalystClass):
        a = AnalystClass()
        result = a.analyze("000001", {"pe": 15, "pb": 1.2, "roe": 20, "momentum_20d": 8})
        assert "analyst" in result
        assert "verdict" in result
        assert "score" in result
        assert "confidence" in result
        assert result["verdict"] in ("推荐", "不推荐", "观望")

    def test_value_analyst_strong_buy(self):
        a = ValueAnalyst()
        r = a.analyze("000001", {"pe": 10, "pb": 1.0, "roe": 25})
        assert r["verdict"] == "推荐"
        assert r["score"] > 0.5

    def test_momentum_analyst_negative(self):
        a = MomentumAnalyst()
        r = a.analyze("000001", {"momentum_20d": -10, "volume_change": -15})
        assert r["verdict"] == "不推荐"
        assert r["score"] < -0.3

    def test_risk_analyst_high_risk(self):
        a = RiskAnalyst()
        r = a.analyze("000001", {"pe": 150, "debt_ratio": 80, "volatility": 0.6})
        assert r["verdict"] == "不推荐"


class TestChairman:
    def test_chairman_init(self):
        c = Chairman()
        assert len(c.analysts) == 7
        assert len(c.weights) == 7

    def test_analyze_recommend(self):
        c = Chairman()
        factor_scores = {
            "pe": 10, "pb": 1.0, "roe": 25,
            "momentum_20d": 8, "volume_change": 15,
            "rsi": 25, "macd": 0.5,
        }
        result = c.analyze("000001", factor_scores)
        assert result.verdict in ("推荐", "不推荐", "观望")
        assert isinstance(result.score, float)
        assert 0 <= result.confidence <= 1
        assert len(result.opinions) == 7

    def test_analyze_watch(self):
        c = Chairman()
        result = c.analyze("000001", {})
        assert result.verdict == "观望"

    def test_analyze_structure(self):
        c = Chairman()
        result = c.analyze("000001", {"pe": 50, "pb": 3, "roe": 5})
        assert isinstance(result.opinions, list)
        for op in result.opinions:
            assert "analyst" in op
            assert "score" in op
            assert "confidence" in op
