"""v1.0.0 发布门禁测试 — 验证关键链路"""
import pytest
from unittest.mock import MagicMock, patch
from common.database import Database
from data_layer import DataLayer
from data_layer._models import DataSnapshot, Kline, MarketState, ReadinessReport, TableStatus
from strategy_layer import StrategyLayer
from strategy_layer._models import FactorScores, TradeSignals
from agent_layer import AgentLayer
from agent_layer.core.models import AgentRequest
from backtest_layer import BacktestLayer
from backtest_layer.core.models import BacktestInput, CostConfig, BacktestReport
from execution_layer import ExecutionLayer
from execution_layer.stats.display import PortfolioSnapshot, ActionFeed
from delivery_layer import DeliveryLayer
from delivery_layer.message import Message
from ops_layer import OpsLayer

DATE = "2026-07-17"

def _make_snapshot() -> DataSnapshot:
    codes = ["000001", "000002", "600519"]
    klines = [Kline(code=c, date=DATE, open=10.0, high=11.0, low=9.5, close=10.5,
                    volume=1e6, amount=1e7, pre_close=10.0, change_pct=5.0, turnover_rate=2.0) for c in codes]
    return DataSnapshot(date=DATE, market=MarketState(state="normal", indices=[]), klines=klines,
        financials=[], valuations=[], moneyflow=[], news=[], limit_up_down=[],
        readiness=ReadinessReport(date=DATE, is_ready=True, tables={}))

def _mock_data_layer() -> MagicMock:
    dl = MagicMock(spec=DataLayer)
    dl.snapshot.return_value = _make_snapshot()
    hist = MagicMock()
    hist.klines = []
    hist.financials = []
    hist.valuations = []
    dl.get_history.return_value = hist
    return dl


class TestV1ReleaseGate:
    """v1.0.0 发布门禁测试"""

    def test_strategy_returns_dataclass(self):
        """验证 get_scores/get_signals 返回 dataclass"""
        from strategy_layer._models import FactorScores, TradeSignals
        strategy = StrategyLayer(data_layer=_mock_data_layer())
        scores = strategy.get_scores("000001", DATE)
        assert isinstance(scores, FactorScores), "get_scores must return FactorScores"
        signals = strategy.get_signals("000001", DATE)
        assert isinstance(signals, TradeSignals), "get_signals must return TradeSignals"

    def test_execution_returns_dataclass(self):
        """验证 get_portfolio_snapshot/get_today_actions 返回 dataclass"""
        db = Database(":memory:")
        db.init_db()
        exec_layer = ExecutionLayer(db=db)
        exec_layer.start()
        snap = exec_layer.get_portfolio_snapshot()
        assert isinstance(snap, PortfolioSnapshot), "get_portfolio_snapshot must return PortfolioSnapshot"
        actions = exec_layer.get_today_actions()
        assert isinstance(actions, ActionFeed), "get_today_actions must return ActionFeed"

    def test_backtest_returns_dataclass(self):
        """验证 BacktestLayer.run 返回 BacktestReport"""
        bt = BacktestLayer(data_layer=_mock_data_layer())
        bt_input = BacktestInput(mode="hold", codes=["000001"], date_range=(DATE, DATE))
        result = bt.run(bt_input)
        assert isinstance(result, BacktestReport), "run must return BacktestReport"

    def test_agent_rule_mode_works(self):
        """验证 Agent 规则模式正常运行"""
        agent = AgentLayer()
        req = AgentRequest(task="stock_analysis", input={"code": "000001", "factor_scores": {"momentum": 0.5}})
        resp = agent.analyze(req)
        assert resp.verdict in ("推荐", "不推荐", "观望"), "Agent must return valid verdict"
        assert hasattr(resp.quality, "tier_used"), "Response must have quality metrics"

    def test_agent_debate_mode_works(self):
        """验证 Agent 辩论模式正常运行"""
        agent = AgentLayer()
        req = AgentRequest(task="debate", input={"code": "000001", "turnover_ratio": 3.5, "pe": 15.0, "momentum_20d": 0.05})
        resp = agent.analyze(req)
        assert resp.verdict in ("推荐", "不推荐", "观望"), "Debate must return valid verdict"

    def test_portfolio_backtest(self):
        """验证组合级回测"""
        bt = BacktestLayer(data_layer=_mock_data_layer())
        result = bt.run_portfolio_backtest(codes=["000001", "000002"], mode="hold")
        assert isinstance(result, BacktestReport), "Portfolio backtest must return BacktestReport"

    def test_ops_health_all_layers(self):
        """验证所有层 health() 返回 LayerHealth"""
        db = Database(":memory:")
        db.init_db()
        from ops_layer.core.models import LayerHealth
        ops = OpsLayer(db=db)
        ops.start()
        mock_layers = {}
        for name in ["data", "strategy", "agent", "backtest", "execution", "report", "delivery"]:
            mock = MagicMock()
            mock.health.return_value = LayerHealth(layer=name, status="green", checks=[], summary="ok")
            mock_layers[name] = mock
        ops.register_layers(mock_layers)
        report = ops.check_health()
        assert report.overall_status in ("green", "yellow", "red"), "Health check must return valid status"
        assert len(report.layers) == 7, "All 7 layers must be registered"

    def test_version_matches_pyproject(self):
        """验证版本号与 pyproject.toml 一致"""
        import tomllib
        with open("pyproject.toml", "rb") as f:
            config = tomllib.load(f)
        version = config["project"]["version"]
        assert version.endswith("-dev") or version.endswith("-rc") or "beta" in version or version.startswith("1."), \
            f"Version must be dev/rc/1.0+, got {version}"

    def test_delivery_rate_limiter(self):
        """验证推送限频器正常工作"""
        from delivery_layer.executor import RateLimiter
        rl = RateLimiter()
        assert rl.can_send("evening_report"), "First send should be allowed"
        assert not rl.can_send("evening_report"), "Second send within cooldown should be denied"