"""9router 集成 — 请求体必须显式 stream=False (防 SSE/多JSON 体解析失败)"""
from agent_layer.llm.client import LLMClient, RateLimiter


def test_request_payload_disables_stream(monkeypatch):
    """9router 默认流式输出, LLMClient 只解析纯 JSON — 必须显式关流."""
    client = LLMClient(providers_path="/nonexistent/path.yaml")
    client._providers = [{"name": "p1", "url": "http://x", "model": "m",
                          "api_key_env": "QS_STREAM_TEST_KEY"}]
    client._rate_limiters["p1"] = RateLimiter(1000)
    monkeypatch.setenv("QS_STREAM_TEST_KEY", "k")

    captured = {}

    class FakeResp:
        status_code = 200
        text = ""

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["json"] = json
        return FakeResp()

    monkeypatch.setattr("agent_layer.llm.client.requests.post", fake_post)
    result = client._chat_impl("hi", None, 0.2, 100, 0, "t", None)
    assert result == "ok"
    assert captured["json"].get("stream") is False
