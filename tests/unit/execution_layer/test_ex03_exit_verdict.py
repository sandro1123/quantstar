"""EX-03: exit_review verdict 对齐 — Agent 退出复盘是参考非闸门

exit_engine.py:71/73 原用字面量 verdict=="approve"/"reject" 比较, 但 AG-03 后
agent 统一返回中文词表 (推荐/不推荐/观望/警惕), 恒不匹配 → 死闸门。
新契约: 消费统一词表, Agent 审核仅作参考不阻断 6 层退出检查; 异常诚实降级。

先写失败测试: legacy "reject" 不再阻断 6 层检查。
"""
from unittest.mock import MagicMock

import pytest

from common.database import Database
from execution_layer.core.exit_engine import ExitEngine


def _make_pool():
    """active 持仓 + 硬止损 19.0, 当前价 18.0 → layer1 必触发"""
    return {
        "code": "000001", "status": "active", "entry_price": 20.0,
        "entry_date": "2026-07-01",  # 非今日, 跳过 T+1
        "stop_loss": 19.0, "take_profit": 0,
    }


def _make_engine(agent_verdict=None, agent_raises=False):
    db = Database(":memory:")
    pool = MagicMock()
    pool.get.return_value = _make_pool()
    agent = MagicMock()
    if agent_raises:
        agent.analyze.side_effect = RuntimeError("agent down")
    else:
        resp = MagicMock()
        resp.verdict = agent_verdict
        agent.analyze.return_value = resp
    engine = ExitEngine(db, pool, agent_layer=agent)
    engine.action_record._ensure_table()
    return engine, agent


@pytest.mark.parametrize("verdict", ["推荐", "不推荐", "观望", "警惕"])
def test_unified_verdicts_do_not_block_six_layer(verdict):
    """四种统一 verdict 均不阻断 6 层检查 — layer1 硬止损照常触发"""
    engine, agent = _make_engine(agent_verdict=verdict)
    result = engine.check_exits("000001", 18.0)
    assert agent.analyze.called, "agent exit_review 应被调用作参考"
    assert result is not None, f"verdict={verdict} 不应阻断退出检查"
    assert result.reason_layer == 1


def test_legacy_reject_no_longer_blocks():
    """失败测试: legacy 'reject' (归一化为 不推荐) 不再阻断 → layer1 触发"""
    engine, agent = _make_engine(agent_verdict="reject")
    result = engine.check_exits("000001", 18.0)
    assert result is not None, "legacy reject 不应阻断 6 层退出检查"
    assert result.reason_layer == 1


def test_legacy_approve_no_longer_special_cased():
    """legacy 'approve' (归一化为 推荐) 也仅作参考, 不改变 6 层结果"""
    engine, agent = _make_engine(agent_verdict="approve")
    result = engine.check_exits("000001", 18.0)
    assert result is not None
    assert result.reason_layer == 1


def test_agent_exception_degrades_honestly():
    """agent 异常 → 诚实降级, 跳过参考继续 6 层检查"""
    engine, agent = _make_engine(agent_raises=True)
    result = engine.check_exits("000001", 18.0)
    assert result is not None, "agent 异常不应阻断退出检查"
    assert result.reason_layer == 1


def test_no_agent_layer_still_checks():
    """无 agent_layer → 直接 6 层检查 (回归)"""
    db = Database(":memory:")
    pool = MagicMock()
    pool.get.return_value = _make_pool()
    engine = ExitEngine(db, pool, agent_layer=None)
    engine.action_record._ensure_table()
    result = engine.check_exits("000001", 18.0)
    assert result is not None
    assert result.reason_layer == 1
