"""Release 0.6.0 集成测试"""
import pytest
from agent_layer import AgentLayer
from agent_layer.core.models import AgentRequest
from agent_layer.debate.models import ExpertReport
from agent_layer.debate.experts import SentimentExpert, RiskExpert
from agent_layer.debate.bull_bear import BullAgent, BearAgent
from agent_layer.debate.fulcrum import Fulcrum
from agent_layer.debate.decision_hub import DecisionHub
from agent_layer.security import InputSanitizer, OutputValidator
from backtest_layer.modes.signal import SignalMode
from backtest_layer.core.benchmark import Benchmark
from backtest_layer.core.broker import Broker
from backtest_layer.core.models import CostConfig


def test_agent_layer_full_flow():
    layer = AgentLayer()
    request = AgentRequest(
        task="stock_analysis",
        input={"code": "000001", "date": "2026-07-17", "factor_scores": {"momentum": 0.5}},
    )
    response = layer.analyze(request)
    assert response.verdict in ("推荐", "不推荐", "观望")
    assert response.quality.latency_ms >= 0


def test_debate_full_flow():
    data = {"code": "000001", "turnover_ratio": 8, "pe": 15, "momentum_20d": 6}
    experts = [cls() for cls in [SentimentExpert, RiskExpert]]
    reports = [e.analyze(data) for e in experts]

    bull = BullAgent()
    bear = BearAgent()
    bull_args = bull.generate(reports)
    bear_args = bear.generate(reports)

    fulcrum = Fulcrum()
    fulcrum_verdict = fulcrum.analyze(reports, bull_args, bear_args)

    hub = DecisionHub()
    decision = hub.decide(fulcrum_verdict, {"composite": 0.5})
    assert decision.verdict in ("推荐", "不推荐", "观望")


def test_security_full_flow():
    sanitizer = InputSanitizer()
    validator = OutputValidator()

    data = {"code": "000001", "text": "分析这只股票"}
    sanitized = sanitizer.sanitize(data)
    assert sanitized["code"] == "000001"

    ok, warnings = validator.validate({"verdict": "推荐", "confidence": 0.7, "pe": 25})
    assert ok is True


def test_backtest_signal_mode():
    broker = Broker(CostConfig(initial_capital=100000))
    mode = SignalMode(broker)
    signals = [
        {"code": "000001", "signal": "buy", "date": "2026-07-17"},
        {"code": "000001", "signal": "hold", "date": "2026-07-18"},
        {"code": "000001", "signal": "sell", "date": "2026-07-19"},
    ]
    prices = {"000001": [15.0, 16.0, 15.5]}
    result = mode.run(signals, prices)
    assert result["mode"] == "signal"
    assert result["trade_count"] >= 1


def test_benchmark_calc():
    b = Benchmark()
    assert abs(b.calc_excess_return(0.15, 0.10) - 0.05) < 1e-10
    ir = b.calc_information_ratio([0.05, 0.03, -0.01])
    assert ir > 0
