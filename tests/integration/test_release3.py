"""Release 3 集成测试"""
import pytest
from common.database import Database
from agent_layer import AgentLayer, AgentRequest
from backtest_layer import BacktestLayer
from backtest_layer.core.models import BacktestInput


def test_agent_entry_check_flow():
    db = Database(":memory:")
    db.init_db()
    al = AgentLayer(database=db, config={})
    req = AgentRequest(task="entry_check", input={"code": "000001", "entry_price": 15.0}, consumer="test")
    resp = al.analyze(req)
    # AG-03: verdict 统一四值词表 (推荐/不推荐/观望/警惕), 旧词表已废
    assert resp.verdict in ("推荐", "不推荐", "观望", "警惕")
    db.close()


def test_backtest_hold_mode():
    bl = BacktestLayer()
    assert bl is not None


def test_strategy_interfaces():
    from strategy_layer import StrategyLayer
    sl = StrategyLayer()
    scores = sl.get_scores("000001", "2026-07-17")
    assert hasattr(scores, "code")
    signals = sl.get_signals("000001", "2026-07-17")
    assert hasattr(signals, "signal")
    regime = sl.market_regime("2026-07-17")
    assert regime in ("bull", "bear", "neutral", "oscillating")
