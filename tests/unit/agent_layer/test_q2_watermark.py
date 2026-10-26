"""Q2 降级留痕+水印 — 规则兜底必带「未调LLM」水印 + LLM 路径留痕 provider"""
from agent_layer.core.agent_layer import AgentLayer
from agent_layer.core.models import AgentRequest
from agent_layer.agents.decision_agent import DecisionAgent
from agent_layer.llm.client import LLMClient, RateLimiter
from common.database import Database


def test_quick_scan_has_watermark():
    """quick_scan 规则路径 reasoning 必含「未调LLM」"""
    db = Database(":memory:")
    al = AgentLayer(database=db, config={})
    req = AgentRequest(
        task="stock_analysis", depth="quick", consumer="test",
        input={"code": "000001", "date": "2026-08-25",
               "factor_scores": {"f1": 0.9, "f2": 0.9}},
    )
    resp = al.analyze(req)
    assert "未调LLM" in resp.reasoning


def test_fallback_response_has_watermark():
    """_fallback_response reasoning 必含「未调LLM」"""
    db = Database(":memory:")
    al = AgentLayer(database=db, config={})
    req = AgentRequest(task="stock_analysis", input={"code": "000001"}, consumer="test")
    resp = al._fallback_response(req, "测试降级")
    assert "未调LLM" in resp.reasoning


def test_decision_agent_rule_fallback_has_watermark():
    """DecisionAgent 无 LLM 时规则兜底 reasoning 必含「未调LLM」"""
    agent = DecisionAgent(provider_manager=None)
    result = agent.analyze({"code": "000001", "factor_scores": {"f1": 0.3}})
    assert "未调LLM" in result.reasoning
    assert result.quality.get("tier_used") == "rule_fallback"


def test_decision_agent_rule_fallback_quality_metadata():
    """DecisionAgent 规则兜底 quality 标记 model_used=rule"""
    agent = DecisionAgent(provider_manager=None)
    result = agent.analyze({"code": "000001", "factor_scores": {"f1": -0.8}})
    assert result.quality.get("model_used") == "rule"


def test_llm_client_last_used_provider():
    """LLMClient 成功调用后 last_used_provider 记录 provider 名"""
    client = LLMClient(providers_path="/nonexistent/path.yaml")
    client._providers = [{"name": "test-prov", "url": "http://x", "model": "m",
                          "api_key_env": "QS_Q2_TEST_KEY"}]
    client._rate_limiters["test-prov"] = RateLimiter(1000)

    import os
    os.environ["QS_Q2_TEST_KEY"] = "k"

    class FakeResp:
        status_code = 200
        text = ""
        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    import agent_layer.llm.client as client_mod
    original_post = client_mod.requests.post
    client_mod.requests.post = lambda *a, **kw: FakeResp()
    try:
        result = client._chat_impl("hi", None, 0.2, 100, 0, "t", None)
        assert result == "ok"
        assert client.last_used_provider == "test-prov"
    finally:
        client_mod.requests.post = original_post
        del os.environ["QS_Q2_TEST_KEY"]
