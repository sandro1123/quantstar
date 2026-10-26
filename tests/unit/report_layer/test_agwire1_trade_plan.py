"""AG-WIRE-1 (2026-09-08): 报告层 agent_trade_plan 档位规范化测试

修点 2: depth="quick_scan" 是管线名非档位值 (合法档 quick/normal/deep),
且 input 里冗余塞了 "depth" 键 — router 按 quick_scan 查不到管线, 落
market_analysis normal 档兜底, 且 depth=quick_scan 恰好命中 AgentLayer
analyze() 的 memory 早退分支 (write_prediction 之前 return, 零留痕嫌疑之一)。
"""
from unittest.mock import MagicMock

from agent_layer.core.registry import get_default_registry
from agent_layer.core.router import AgentRouter

from report_layer.sections.agent_trade_plan import AgentTradePlanSection


def _make_context(agent_layer, date="2026-09-08"):
    ctx = MagicMock()
    ctx.agent_layer = agent_layer
    ctx.date = date
    return ctx


def test_trade_plan_requests_legal_depth():
    """AG-WIRE-1: AgentRequest.depth 必须是合法档位 (quick/normal/deep), 非 quick_scan"""
    agent = MagicMock()
    agent.analyze.return_value.verdict = "观望"
    agent.analyze.return_value.confidence = 0.5
    agent.analyze.return_value.reasoning = "ok"
    agent.analyze.return_value.risk_flags = []
    ctx = _make_context(agent)
    AgentTradePlanSection().build(ctx)
    agent.analyze.assert_called_once()
    req = agent.analyze.call_args[0][0]
    assert req.depth in ("quick", "normal", "deep"), \
        f"quick_scan 是管线名非档位值: got {req.depth}"
    assert req.depth == "quick"


def test_trade_plan_no_redundant_depth_in_input():
    """AG-WIRE-1: input 里不再冗余塞 depth 键 (请求规范: depth 是 AgentRequest 字段)"""
    agent = MagicMock()
    agent.analyze.return_value.verdict = "观望"
    agent.analyze.return_value.confidence = 0.5
    agent.analyze.return_value.reasoning = "ok"
    agent.analyze.return_value.risk_flags = []
    ctx = _make_context(agent)
    AgentTradePlanSection().build(ctx)
    req = agent.analyze.call_args[0][0]
    assert "depth" not in req.input, "input 冗余 depth 键必须删除"


def test_trade_plan_request_resolves_to_registered_pipeline():
    """AG-WIRE-1: quick 档请求真解析到 market_analysis quick 管线 (10s 档)"""
    agent = MagicMock()
    agent.analyze.return_value.verdict = "观望"
    agent.analyze.return_value.confidence = 0.5
    agent.analyze.return_value.reasoning = "ok"
    agent.analyze.return_value.risk_flags = []
    ctx = _make_context(agent)
    AgentTradePlanSection().build(ctx)
    req = agent.analyze.call_args[0][0]
    name, timeout = AgentRouter(provider_manager=None,
                                registry=get_default_registry()).resolve(req)
    assert name == "market_analysis"
    assert timeout == 10.0, "quick 档应命中 registry 里 market_analysis quick (10s)"


def test_trade_plan_payload_keeps_market_code_and_date():
    """AG-WIRE-1: payload 契约不变 — 000001.SH + 当日 date (handler 必需键)"""
    agent = MagicMock()
    agent.analyze.return_value.verdict = "观望"
    agent.analyze.return_value.confidence = 0.5
    agent.analyze.return_value.reasoning = "ok"
    agent.analyze.return_value.risk_flags = []
    ctx = _make_context(agent, date="2026-09-08")
    AgentTradePlanSection().build(ctx)
    req = agent.analyze.call_args[0][0]
    assert req.task == "market_analysis"
    assert req.input["code"] == "000001.SH"
    assert req.input["date"] == "2026-09-08"


def test_trade_plan_section_renders_verdict():
    """AG-WIRE-1: 修后 section 渲染面不回归 (verdict/置信度/仓位仍上报告)"""
    agent = MagicMock()
    resp = MagicMock()
    resp.verdict = "观望"
    resp.confidence = 0.62
    resp.reasoning = "市场信号中性"
    resp.risk_flags = []
    resp.output = {"position_pct": 0.5}
    agent.analyze.return_value = resp
    ctx = _make_context(agent)
    result = AgentTradePlanSection().build(ctx)
    assert result is not None
    assert "观望" in result.body
    assert "62.0%" in result.body
    assert "50%" in result.body
