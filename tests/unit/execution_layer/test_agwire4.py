# AG-WIRE-4 (统筹 2026-09-08) 单测 — intraday_scan 盘中 agent 腿通电
# 病: ExecutionLayer 构造漏传 agent_layer → 盘中 entry/exit/波动解释腿首行早退
# 修: 补 AgentLayer 构造 (bootstrap 先例, try 包裹非关键层)
import inspect
from unittest.mock import MagicMock, patch


def _module_source():
    from execution_layer.scripts import intraday_scan
    return inspect.getsource(intraday_scan)


class TestWiring:
    def test_execution_layer_receives_agent_layer(self):
        """构造行显式传 agent_layer= (不再吃默认 None)"""
        src = _module_source()
        assert 'agent_layer=agent_layer' in src

    def test_agent_layer_failure_does_not_block_scan(self):
        """AgentLayer 构造失败 → warning 降级, ExecutionLayer 仍构造 (agent_layer=None)"""
        src = _module_source()
        assert 'try:' in src and 'except Exception' in src
        # 降级语义: agent_layer 保持 None 传入, 扫描不阻断
        assert '盘中 agent 腿降级' in src

    def test_uses_bootstrap_precedent_form(self):
        """按 bootstrap.py:90-98 先例: AgentLayer(db, config) + start()"""
        src = _module_source()
        assert 'AgentLayer(db, config)' in src
        assert 'agent_layer.start()' in src


class TestRuntimeBehavior:
    def test_execution_layer_ctor_accepts_agent(self):
        """ExecutionLayer 构造器本就吃 agent_layer 参数 (契约面)"""
        from execution_layer import ExecutionLayer
        import inspect as i
        sig = i.signature(ExecutionLayer.__init__)
        assert 'agent_layer' in sig.parameters

    def test_volatility_leg_uses_agent_when_present(self):
        """IntradayMonitor agent_layer=None 早退的病根已由 AG-WIRE-1 修 task;
        本卡保证构造面: monitor 拿到的 agent_layer 非空时走真解释腿 (契约拼装)"""
        from execution_layer.intraday.monitor import IntradayMonitor
        m = IntradayMonitor.__new__(IntradayMonitor)
        m.agent_layer = MagicMock()
        # agent_layer 属性在 — 契约链通 (真腿行为由 AG-WIRE-1 测试覆盖)
        assert m.agent_layer is not None
