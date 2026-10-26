"""AG-11: market_analysis 管线 — MarketAgent + registry 注册 + dispatch 接线 测试"""
from agent_layer.agents.market_agent import MarketAgent
from agent_layer.core.models import AgentRequest
from agent_layer.core.registry import get_default_registry


class FakeMarket:
    def __init__(self, state="neutral", up=3000, down=2000, temp=0.8):
        self.state = state
        self.up_count = up
        self.down_count = down
        self.market_temperature = temp


class FakeSentiment:
    def __init__(self, zt=60, dt=5):
        self.zt_count = zt
        self.dt_count = dt
        self.limit_up_count = zt
        self.limit_down_count = dt
        self.market_temp = 0.5


class FakeKline:
    def __init__(self, amount):
        self.amount = amount


class FakeSnapshot:
    def __init__(self, market, klines):
        self.market = market
        self.klines = klines


class FakeDataLayer:
    def __init__(self, market=None, sentiment=None, today_amount=1e12, prev_amount=0.8e12):
        self._market = market
        self._sentiment = sentiment
        self._today = today_amount
        self._prev = prev_amount

    def snapshot(self, date):
        return FakeSnapshot(self._market, [FakeKline(100)])

    def get_sentiment_data(self, date):
        return self._sentiment

    def snapshot_range(self, start, end):
        # 第一次调用返回今日, 之后返回前一日
        if not hasattr(self, "_called"):
            self._called = True
            return [FakeSnapshot(self._market, [FakeKline(self._today)])]
        return [FakeSnapshot(self._market, [FakeKline(self._prev)])]


def test_registry_has_market_analysis_pipeline():
    """AG-11: registry 注册了 market_analysis 管线 (normal + quick)"""
    reg = get_default_registry()
    pipelines = reg.get_pipelines("market_analysis")
    assert len(pipelines) >= 2
    depths = {p.depth for p in pipelines}
    assert "normal" in depths
    assert "quick" in depths


def test_market_agent_bullish():
    """AG-11: 涨多跌少 + 涨停活跃 → 推荐 (统一词表, 市场级=看多)"""
    data = FakeDataLayer(market=FakeMarket(up=3500, down=1500, temp=2.0),
                         sentiment=FakeSentiment(zt=60, dt=5))
    agent = MarketAgent(data_layer=data, provider_manager=None)
    result = agent.analyze({"date": "2026-08-18"})
    assert result.verdict == "推荐"
    assert result.confidence > 0.5
    assert result.output["position_pct"] > 0.5
    assert result.quality["model_used"] == "rule"


def test_market_agent_bearish():
    """AG-11: 跌多涨少 + 跌停恐慌 → 不推荐 (统一词表, 市场级=看空)"""
    data = FakeDataLayer(market=FakeMarket(up=1000, down=4000, temp=-2.0),
                         sentiment=FakeSentiment(zt=5, dt=40))
    agent = MarketAgent(data_layer=data, provider_manager=None)
    result = agent.analyze({"date": "2026-08-18"})
    assert result.verdict == "不推荐"
    assert result.output["position_pct"] < 0.5


def test_market_agent_neutral():
    """AG-11: 均衡信号 → 观望 (统一词表, 市场级=震荡无方向)"""
    data = FakeDataLayer(market=FakeMarket(up=2500, down=2500, temp=0.1),
                         sentiment=FakeSentiment(zt=20, dt=15))
    agent = MarketAgent(data_layer=data, provider_manager=None)
    result = agent.analyze({"date": "2026-08-18"})
    assert result.verdict == "观望"


def test_market_agent_no_data_layer():
    """AG-11: 无 DataLayer → 诚实降级观望, 不伪造"""
    agent = MarketAgent(data_layer=None, provider_manager=None)
    result = agent.analyze({"date": "2026-08-18"})
    assert result.verdict == "观望"
    assert result.quality["tier_used"] == "rule_fallback"


def test_market_agent_volume_signal():
    """AG-11: 量能对比真实计算 (今日/前日)"""
    data = FakeDataLayer(market=FakeMarket(up=3500, down=1500, temp=2.0),
                         sentiment=None, today_amount=1.5e12, prev_amount=1.0e12)
    agent = MarketAgent(data_layer=data, provider_manager=None)
    result = agent.analyze({"date": "2026-08-18"})
    assert result.output["volume_ratio"] == 1.5
    assert "放量" in result.reasoning


def test_dispatch_routes_market_analysis():
    """AG-11: _execute_pipeline 把 market_analysis 路由到 _market_analysis"""
    from agent_layer.core.agent_layer import AgentLayer
    al = AgentLayer.__new__(AgentLayer)
    al._data_layer = FakeDataLayer(market=FakeMarket(up=3500, down=1500, temp=2.0),
                                   sentiment=FakeSentiment(zt=60, dt=5))
    al._pm = None
    req = AgentRequest(task="market_analysis", input={"date": "2026-08-18", "code": "MARKET"})
    resp = al._execute_pipeline(req, "market_analysis")
    assert resp.verdict == "推荐"
    assert resp.agents_used == ["market_agent"]
    assert resp.quality.model_used == "rule"


def test_report_consumer_task_now_resolves():
    """AG-11: AgentTradePlanSection 用的 task=market_analysis 不再静默落 quick_scan"""
    from agent_layer.core.router import AgentRouter
    reg = get_default_registry()
    router = AgentRouter(provider_manager=None, registry=reg)
    req = AgentRequest(task="market_analysis", input={"date": "2026-08-18"}, depth="quick_scan")
    name, timeout = router.resolve(req)
    assert name == "market_analysis"
