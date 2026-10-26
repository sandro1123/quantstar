"""AG-WIRE-1 (2026-09-08): 执行层 agent 调用点规范化测试

三修点:
  1. monitor 市场型波动解释 — market_volatility 未注册 task → 改走已注册
     market_analysis (AG-11, quick 档), 不再吃 router quick_scan 兜底假回复
  3. entry/exit 引擎审核 — depth 显式 quick (价格风控面 5s 档足够)
"""
from unittest.mock import MagicMock

from common.database import Database
from execution_layer.core.entry_engine import EntryEngine
from execution_layer.core.exit_engine import ExitEngine
from execution_layer.intraday.monitor import IntradayMonitor


# ============ 修点 1: 市场型波动解释 ============

def _make_monitor(agent):
    db = Database(":memory:")
    pool = MagicMock()
    monitor = IntradayMonitor(db, pool, agent_layer=agent)
    return monitor


def test_volatility_market_type_uses_registered_market_analysis():
    """AG-WIRE-1: 市场型波动解释发 market_analysis (已注册) + 显式 depth=quick

    修前: task=market_volatility 不在 registry 12 管线里, router 兜底落
    quick_scan 假回复 (composite=0 观望「未调LLM」)。
    """
    agent = MagicMock()
    agent.analyze.return_value = MagicMock(result="市场分析", output="市场分析")
    monitor = _make_monitor(agent)
    monitor._generate_volatility_explanation(None, -4.2, "market")
    agent.analyze.assert_called_once()
    req = agent.analyze.call_args[0][0]
    assert req.task == "market_analysis"
    assert req.depth == "quick"


def test_volatility_market_request_payload_matches_handler_contract():
    """AG-WIRE-1: 市场型 payload 按 _market_analysis handler 期望适配

    handler 读 input['date'] (无 date → MarketAgent 查空日数据降级);
    code 用 handler 默认 'MARKET' (None 会炸 write_prediction 的 code NOT NULL);
    跌幅信息 (drop_pct/type) 必须保留 — 派单要求不丢上下文。
    """
    agent = MagicMock()
    agent.analyze.return_value = MagicMock(result="市场分析", output="市场分析")
    monitor = _make_monitor(agent)
    monitor._generate_volatility_explanation(None, -4.2, "market")
    req = agent.analyze.call_args[0][0]
    # handler 契约键
    assert req.input.get("date"), "handler 读 date 键, 必须带当日"
    assert req.input.get("code") == "MARKET"
    # 跌幅信息保留
    assert req.input["drop_pct"] == 4.2
    assert req.input["type"] == "market"


def test_volatility_market_request_resolves_to_market_pipeline():
    """AG-WIRE-1: 市场型请求经 router 真解析到 market_analysis 管线 (非 quick_scan 兜底)"""
    from agent_layer.core.models import AgentRequest
    from agent_layer.core.registry import get_default_registry
    from agent_layer.core.router import AgentRouter
    agent = MagicMock()
    agent.analyze.return_value = MagicMock(result="市场分析", output="市场分析")
    monitor = _make_monitor(agent)
    monitor._generate_volatility_explanation(None, -4.2, "market")
    req = agent.analyze.call_args[0][0]
    router = AgentRouter(provider_manager=None, registry=get_default_registry())
    name, timeout = router.resolve(req)
    assert name == "market_analysis", f"修前落 quick_scan 兜底, 修后必须真解析: got {name}"


def test_volatility_market_request_dispatches_to_market_agent():
    """AG-WIRE-1: 市场型请求端到端落 MarketAgent handler (mock 断言真 handler 被调)

    捕获 monitor 发出的请求 → 真链路 dispatch → resp.agents_used=['market_agent']
    (quick_scan 兜底的 agents_used=['quick_scan'], 此处即「未走假回复」判读键)。
    """
    from agent_layer.core.agent_layer import AgentLayer
    from agent_layer.core.registry import get_default_registry
    from agent_layer.core.router import AgentRouter
    from tests.unit.agent_layer.test_ag11_market_analysis import FakeDataLayer, FakeMarket, FakeSentiment

    agent = MagicMock()
    agent.analyze.return_value = MagicMock(result="市场分析", output="市场分析")
    monitor = _make_monitor(agent)
    monitor._generate_volatility_explanation(None, -4.2, "market")
    req = agent.analyze.call_args[0][0]

    # 真链路: router 解析 + dispatch 落点
    al = AgentLayer.__new__(AgentLayer)
    al._data_layer = FakeDataLayer(market=FakeMarket(up=3500, down=1500, temp=2.0),
                                   sentiment=FakeSentiment(zt=60, dt=5))
    al._pm = None
    name, _timeout = AgentRouter(provider_manager=None,
                                 registry=get_default_registry()).resolve(req)
    assert name == "market_analysis"
    resp = al._execute_pipeline(req, name)
    assert resp.agents_used == ["market_agent"], \
        f"必须走 MarketAgent handler: got {resp.agents_used}"
    assert resp.quality.model_used == "rule"


def test_volatility_stock_type_unchanged():
    """AG-WIRE-1: 个股型波动解释不动 (已注册 task + 合法档位, 派单未点名)"""
    agent = MagicMock()
    agent.analyze.return_value = MagicMock(result="个股分析", output="个股分析")
    monitor = _make_monitor(agent)
    monitor._generate_volatility_explanation("000001", -4.2, "stock")
    agent.analyze.assert_called_once()
    req = agent.analyze.call_args[0][0]
    assert req.task == "stock_analysis"
    assert req.input["code"] == "000001"
    assert req.input["drop_pct"] == 4.2


# ============ 修点 3: entry/exit 显式 depth=quick ============

def _entry_pool():
    pool = MagicMock()
    pool.get.return_value = {"code": "000001", "status": "pending_entry",
                             "entry_low": 14.0, "entry_high": 16.0, "exit_date": None}
    pool.get_available_cash.return_value = 300000.0
    return pool


def test_entry_engine_agent_request_declares_quick_depth():
    """AG-WIRE-1: 入场审核显式 depth=quick (价格风控面, 5s 档足够)

    修前: depth 未声明吃 AgentRequest 默认 normal → 命中 entry_check normal 管线
    (10s 超时档), 与盘中 5s 风控节奏不符。
    """
    agent = MagicMock()
    agent.analyze.return_value.verdict = "推荐"
    engine = EntryEngine(Database(":memory:"), _entry_pool(), agent_layer=agent)
    result = engine.check_entry("000001", 15.0, "09:35:00")
    assert result is not None
    agent.analyze.assert_called_once()
    req = agent.analyze.call_args[0][0]
    assert req.task == "entry_check"
    assert req.depth == "quick", "修前吃默认 normal, 修后必须显式 quick"


def test_exit_engine_agent_request_declares_quick_depth():
    """AG-WIRE-1: 出场复盘审核显式 depth=quick (参考面不阻断 6 层检查, 5s 档足够)"""
    agent = MagicMock()
    agent.analyze.return_value.verdict = "推荐"
    agent.analyze.return_value.reasoning = "ok"
    pool = MagicMock()
    pool.get.return_value = {"code": "000001", "status": "active",
                             "entry_price": 15.0, "entry_date": "2026-07-01"}
    engine = ExitEngine(Database(":memory:"), pool, agent_layer=agent)
    engine.check_exits("000001", 14.0)
    agent.analyze.assert_called_once()
    req = agent.analyze.call_args[0][0]
    assert req.task == "exit_review"
    assert req.depth == "quick", "修前吃默认 normal, 修后必须显式 quick"


def test_entry_quick_depth_hits_quick_pipeline():
    """AG-WIRE-1: entry quick 档请求经 router 解析到 5s quick 管线 (超时档对表)"""
    from agent_layer.core.registry import get_default_registry
    from agent_layer.core.router import AgentRouter
    from agent_layer.core.models import AgentRequest
    agent = MagicMock()
    agent.analyze.return_value.verdict = "推荐"
    engine = EntryEngine(Database(":memory:"), _entry_pool(), agent_layer=agent)
    engine.check_entry("000001", 15.0, "09:35:00")
    req = agent.analyze.call_args[0][0]
    name, timeout = AgentRouter(provider_manager=None,
                                registry=get_default_registry()).resolve(req)
    assert name == "entry_check"
    assert timeout == 5.0, "quick 档必须配 5s 超时管线"


def test_exit_quick_depth_hits_quick_pipeline():
    """AG-WIRE-1: exit quick 档请求经 router 解析到 5s quick 管线 (超时档对表)"""
    from agent_layer.core.registry import get_default_registry
    from agent_layer.core.router import AgentRouter
    agent = MagicMock()
    agent.analyze.return_value.verdict = "推荐"
    agent.analyze.return_value.reasoning = "ok"
    pool = MagicMock()
    pool.get.return_value = {"code": "000001", "status": "active",
                             "entry_price": 15.0, "entry_date": "2026-07-01"}
    engine = ExitEngine(Database(":memory:"), pool, agent_layer=agent)
    engine.check_exits("000001", 14.0)
    req = agent.analyze.call_args[0][0]
    name, timeout = AgentRouter(provider_manager=None,
                                registry=get_default_registry()).resolve(req)
    assert name == "exit_review"
    assert timeout == 5.0, "quick 档必须配 5s 超时管线"
