"""Agent Layer 单元测试"""
import pytest
import time
from common.database import Database
from agent_layer import AgentLayer, AgentRequest
from agent_layer.core.models import AgentState, AgentRequest as AR
from agent_layer.core.hooks import PipelineHook, LoggingHook
from agent_layer.quality.memory import AgentMemory


@pytest.fixture
def agent_layer():
    db = Database(":memory:")
    al = AgentLayer(database=db, config={})
    yield al
    db.close()


def test_entry_check_approve(agent_layer):
    req = AgentRequest(task="entry_check", input={"code": "000001", "entry_price": 15.0}, consumer="test")
    resp = agent_layer.analyze(req)
    assert resp.verdict == "推荐"  # AG-03: 统一词表, 审核通过=推荐
    assert resp.confidence >= 0


def test_entry_check_reject_invalid_price(agent_layer):
    req = AgentRequest(task="entry_check", input={"code": "000001", "entry_price": -1}, consumer="test")
    resp = agent_layer.analyze(req)
    assert resp.verdict == "不推荐"  # AG-03: 统一词表, 审核拒绝=不推荐


def test_entry_check_reject_blacklist(agent_layer):
    req = AgentRequest(task="entry_check", input={"code": "ST0001", "entry_price": 10.0}, consumer="test")
    resp = agent_layer.analyze(req)
    assert resp.verdict == "不推荐"


def test_exit_review(agent_layer):
    req = AgentRequest(task="exit_review", input={"code": "000001", "exit_price": 14.0, "entry_price": 15.0}, consumer="test")
    resp = agent_layer.analyze(req)
    assert resp.verdict in ("推荐", "不推荐", "观望", "警惕")  # AG-03: 统一枚举
    assert resp.confidence >= 0


def test_agent_layer_health(agent_layer):
    health = agent_layer.health()
    assert health.layer == "agent"
    assert health.status in ("green", "yellow", "red")


def test_agent_layer_start(agent_layer):
    agent_layer.start()
    assert True


def test_agent_layer_shutdown(agent_layer):
    agent_layer.shutdown()
    assert True


def test_agent_layer_cache(agent_layer):
    req = AgentRequest(task="entry_check", input={"code": "000001", "entry_price": 15.0}, consumer="test")
    resp1 = agent_layer.analyze(req)
    resp2 = agent_layer.analyze(req)
    assert resp1.verdict == resp2.verdict


def test_agent_layer_invalidate_cache(agent_layer):
    req = AgentRequest(task="entry_check", input={"code": "000001", "entry_price": 15.0}, consumer="test")
    agent_layer.analyze(req)
    agent_layer.invalidate_for_news("000001", "重大利好")
    assert len(agent_layer._cache) == 0


def test_agent_layer_invalidate_cache_specific_code(agent_layer):
    req1 = AgentRequest(task="entry_check", input={"code": "000001", "entry_price": 15.0}, consumer="test")
    req2 = AgentRequest(task="entry_check", input={"code": "000002", "entry_price": 20.0}, consumer="test")
    agent_layer.analyze(req1)
    agent_layer.analyze(req2)
    agent_layer.invalidate_cache("entry_check", "000001")
    # 000001 的缓存应该被清空，但 000002 的缓存应该保留
    remaining = [k for k in agent_layer._cache if "000002" in k]
    assert len(remaining) > 0, "特定代码失效不应清空其他代码的缓存"


def test_agent_layer_batch(agent_layer):
    reqs = [
        AgentRequest(task="quick_check", input={"code": "000001"}, consumer="test"),
        AgentRequest(task="quick_check", input={"code": "000002"}, consumer="test"),
    ]
    results = agent_layer.analyze_batch(reqs)
    assert len(results) == 2


def test_agent_layer_fallback_on_timeout(agent_layer):
    req = AgentRequest(task="stock_analysis", input={"code": "000001", "factor_scores": {}}, consumer="test", depth="deep")
    resp = agent_layer.analyze(req)
    assert resp.verdict in ("推荐", "不推荐", "观望")


def test_agent_state():
    req = AR(task="test", input={"code": "000001"})
    state = AgentState(request=req)
    assert state.version == 1
    state.add_error("test error")
    assert len(state.errors) == 1
    assert state.latest_response is None


def test_agent_memory():
    db = Database(":memory:")
    memory = AgentMemory(database=db)
    memory.remember("000001", "2026-07-17", "推荐", 0.8, "好股票")
    rec = memory.recall("000001")
    assert rec is not None
    assert rec["verdict"] == "推荐"
    history = memory.recall_history("000001")
    assert len(history) == 1
    db.close()


def test_pipeline_hook():
    hook = PipelineHook()
    req = AR(task="test", input={"code": "000001"})
    hook.before_execute(req)
    from agent_layer.core.models import AgentResponse, QualityMetrics
    resp = AgentResponse(verdict="推荐", confidence=0.8, quality=QualityMetrics())
    hook.after_execute(req, resp)
    hook.on_error(req, Exception("test"))
    assert True


def test_logging_hook():
    hook = LoggingHook()
    req = AR(task="test", input={"code": "000001"}, consumer="test")
    from agent_layer.core.models import AgentResponse, QualityMetrics
    resp = AgentResponse(verdict="推荐", confidence=0.8, quality=QualityMetrics())
    hook.after_execute(req, resp)
    assert True


def test_format_as_text_with_risk_flags(agent_layer):
    from agent_layer.core.models import AgentResponse, QualityMetrics, RiskFlag
    resp = AgentResponse(
        code="000001", verdict="推荐", confidence=0.8, reasoning="基本面好",
        risk_flags=[RiskFlag(flag="估值偏高", severity="medium")],
        quality=QualityMetrics(),
    )
    text = agent_layer._format_as_text(resp)
    assert "估值偏高" in text


def test_format_as_html_with_risk_flags(agent_layer):
    from agent_layer.core.models import AgentResponse, QualityMetrics, RiskFlag
    resp = AgentResponse(
        code="000001", verdict="推荐", confidence=0.8, reasoning="基本面好",
        risk_flags=[RiskFlag(flag="估值偏高", severity="medium")],
        quality=QualityMetrics(),
    )
    html = agent_layer._format_as_html(resp)
    assert "估值偏高" in html


class TestShutdownGraceful:
    """回归: 067-29 日志 15 次 'cannot schedule new futures after shutdown'
    — analyze/analyze_batch/analyze_async 在 shutdown 后调用必须降级返回,
    不得继续向已关闭 executor submit。"""

    def test_analyze_after_shutdown_degrades(self, agent_layer):
        agent_layer.shutdown()
        assert agent_layer._is_shutting_down is True
        req = AgentRequest(task="stock_analysis", input={"code": "000001"}, consumer="test")
        resp = agent_layer.analyze(req)
        assert resp.verdict == "观望"
        assert resp.quality.tier_used == "degraded"
        assert "关闭" in resp.reasoning

    def test_analyze_batch_after_shutdown_degrades(self, agent_layer):
        agent_layer._is_shutting_down = True
        reqs = [AgentRequest(task="quick_check", input={"code": "000001"}, consumer="test")]
        results = agent_layer.analyze_batch(reqs)
        assert len(results) == 1
        assert results[0].verdict == "观望"

    def test_analyze_async_after_shutdown_degrades(self, agent_layer):
        import asyncio
        agent_layer._is_shutting_down = True
        req = AgentRequest(task="stock_analysis", input={"code": "000001"}, consumer="test")
        resp = asyncio.run(agent_layer.analyze_async(req))
        assert resp.verdict == "观望"

    def test_shutdown_is_idempotent(self, agent_layer):
        agent_layer.shutdown()
        agent_layer.shutdown()  # 第二次 shutdown 不应抛异常
        assert agent_layer._is_shutting_down is True

def test_router_resolves_normal_analysis_for_stock(agent_layer):
    """Blocker 2 回归：stock_analysis + normal depth 必须路由到 normal_analysis（LLM 管道），
    而非回退到纯规则的 quick_scan"""
    from agent_layer.core.models import AgentRequest as R
    req = R(task="stock_analysis", input={"code": "600519"}, depth="normal")
    pipeline, timeout = agent_layer._router.resolve(req)
    assert pipeline == "normal_analysis"
    assert timeout >= 30


def test_router_resolves_quick_scan_for_quick(agent_layer):
    req = AR(task="stock_analysis", input={"code": "600519"}, depth="quick")
    pipeline, _ = agent_layer._router.resolve(req)
    assert pipeline == "quick_scan"
