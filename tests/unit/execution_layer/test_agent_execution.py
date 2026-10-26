"""Execution Layer 集成测试 — Agent 接入"""
from unittest.mock import MagicMock
from common.database import Database
from execution_layer.core.entry_engine import EntryEngine
from execution_layer.core.exit_engine import ExitEngine


def test_entry_engine_agent_approves():
    db = Database(":memory:")
    pool = MagicMock()
    pool.get.return_value = {"code": "000001", "status": "pending_entry", "entry_low": 14.0, "entry_high": 16.0, "exit_date": None}
    pool.get_available_cash.return_value = 300000.0

    agent = MagicMock()
    agent.analyze.return_value.verdict = "approve"

    engine = EntryEngine(db, pool, agent_layer=agent)
    result = engine.check_entry("000001", 15.0, "09:35:00")
    assert result is not None
    assert result.action == "buy"


def test_entry_engine_agent_rejects():
    db = Database(":memory:")
    pool = MagicMock()
    pool.get.return_value = {"code": "000001", "status": "pending_entry", "entry_low": 14.0, "entry_high": 16.0, "exit_date": None}
    pool.get_available_cash.return_value = 300000.0

    agent = MagicMock()
    agent.analyze.return_value.verdict = "不推荐"  # AG-03: 统一词表

    engine = EntryEngine(db, pool, agent_layer=agent)
    result = engine.check_entry("000001", 15.0, "09:35:00")
    assert result is None


def test_entry_engine_no_agent():
    db = Database(":memory:")
    pool = MagicMock()
    pool.get.return_value = {"code": "000001", "status": "pending_entry", "entry_low": 14.0, "entry_high": 16.0, "exit_date": None}
    pool.get_available_cash.return_value = 300000.0

    engine = EntryEngine(db, pool, agent_layer=None)
    result = engine.check_entry("000001", 15.0, "09:35:00")
    assert result is not None
    assert result.action == "buy"


def test_exit_engine_agent_approves():
    db = Database(":memory:")
    pool = MagicMock()
    pool.get.return_value = {"code": "000001", "status": "active", "entry_price": 15.0}

    agent = MagicMock()
    agent.analyze.return_value.verdict = "approve"

    engine = ExitEngine(db, pool, agent_layer=agent)
    result = engine.check_exits("000001", 14.0)
    assert result is None or result.action in ("sell", "exit")


def test_exit_engine_agent_rejects():
    db = Database(":memory:")
    pool = MagicMock()
    pool.get.return_value = {"code": "000001", "status": "active", "entry_price": 15.0}

    agent = MagicMock()
    agent.analyze.return_value.verdict = "不推荐"  # AG-03: 统一词表

    engine = ExitEngine(db, pool, agent_layer=agent)
    result = engine.check_exits("000001", 14.0)
    assert result is None


def test_exit_engine_no_agent():
    db = Database(":memory:")
    pool = MagicMock()
    pool.get.return_value = {"code": "000001", "status": "active", "entry_price": 15.0}

    engine = ExitEngine(db, pool, agent_layer=None)
    result = engine.check_exits("000001", 14.0)
    assert result is None or result.action in ("sell", "exit")