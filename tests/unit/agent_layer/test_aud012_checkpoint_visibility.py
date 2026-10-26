"""AUD-012 回归: agent 检查点读写失败必须显形, 不得 except: pass。

检查点存在的意义是崩溃恢复。写失败静默 -> 恢复时无可恢复;
读失败静默 -> 从零重来且无任何信号 (state_machine.py _persist / recover
两处都是裸 `except Exception: pass`)。

故障面构造: 先用真库正常构造 (StateMachine.__init__ 会建表), 构造完成后
让 get_connection 抛错 —— 对应生产里"撞写锁 / 连接被调用方 close"的真实形态。
"""
import logging

import pytest

from common.database import Database
from agent_layer.core.state_machine import StateMachine


@pytest.fixture
def sm_then_broken():
    sm = StateMachine(db=Database(":memory:"))      # 构造期正常 (建表)

    def _boom():
        raise RuntimeError("database is locked")

    sm.db.get_connection = _boom                     # 此后一切 DB 访问失败
    return sm


def test_checkpoint_save_failure_is_logged(sm_then_broken, caplog):
    with caplog.at_level(logging.WARNING):
        key = sm_then_broken.create("agent-x", "task-y")
    assert key, "写失败不应让 create 崩溃 (内存态仍可用)"
    assert "checkpoint" in caplog.text.lower(), (
        "检查点写失败必须留痕, 否则崩溃恢复时无可恢复且无人知")


def test_checkpoint_recover_failure_is_logged(sm_then_broken, caplog):
    with caplog.at_level(logging.WARNING):
        got = sm_then_broken.recover("some-key")
    assert got is None
    assert "checkpoint" in caplog.text.lower(), "检查点读失败必须留痕"
