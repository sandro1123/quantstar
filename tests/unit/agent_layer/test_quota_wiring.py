"""微卡: ops.agent.quota 五键接线半程收口 — TDD (红先行)。

病 (09-12 配置普查对拍): quota_manager.configure 参数与 ops.yaml agent.quota
五键逐字对齐, 但生产面零调用 — provider_manager 构造 QuotaManager() 后
走 _DEFAULT_SOURCE_PATH 模块级同值硬编码兜底 = 默合假象 (yaml 改值不生效)。
键语义漂移活标本 (比死键更隐蔽)。

修法 (统筹 GO sm-05bb9669): ProviderManager 构造点 cfg_get 五键直灌
configure (D-24 execution.initial_capital 先例同款); 双锚测试; 兜底保底
不删+权威指源注释。
"""
import yaml

import pytest


class _FakeCfg:
    """config 先例同款 (agent_layer.py:43 cfg_get 形态)"""
    def __init__(self, values):
        self._v = values
    def get(self, key, default=None):
        return self._v.get(key, default)


class TestQuotaWiredAtConstruction:
    """锚1: 构造点接线 — cfg 五键 → quota_manager 实例值"""

    def _make_pm(self, overrides=None):
        from agent_layer.llm.provider_manager import ProviderManager
        cfg = _FakeCfg(overrides or {})
        return ProviderManager(config=cfg)

    def test_yaml_values_flow_into_instance(self):
        """通电锚: yaml 改值 → 实例值变 (防默合假象复发)"""
        pm = self._make_pm({
            "ops.agent.quota.daily_budget_requests": 777,
            "ops.agent.quota.source_path": "/tmp/alt.sqlite",
        })
        assert pm.quota_manager._daily_budget_requests == 777
        assert pm.quota_manager._source_path == "/tmp/alt.sqlite"

    def test_low_remaining_pct_and_cost_flow(self):
        pm = self._make_pm({
            "ops.agent.quota.daily_budget_cost": 9.5,
            "ops.agent.quota.low_remaining_pct": 0.15,
            "ops.agent.quota.api_key_hint": "sk-test",
        })
        assert pm.quota_manager._daily_budget_cost == 9.5
        assert pm.quota_manager._low_remaining_pct == 0.15
        assert pm.quota_manager._api_key_hint == "sk-test"

    def test_no_config_falls_back_to_defaults(self):
        """config 缺省 → 实例 _source_path=None, 读时走 _DEFAULT 兜底
        (quota_manager.py:111 `or _DEFAULT_SOURCE_PATH` 读时降级语义不变)"""
        from agent_layer.llm.provider_manager import ProviderManager
        pm = ProviderManager()
        assert pm.quota_manager._source_path is None  # 未灌值
        # 读时兜底: _today_usage 里 path = None or _DEFAULT
        from agent_layer.llm import quota_manager as qm_mod
        assert pm.quota_manager._source_path or qm_mod._DEFAULT_SOURCE_PATH == \
            qm_mod._DEFAULT_SOURCE_PATH

    def test_production_yaml_values_match(self):
        """生产面锚: 真 ops.yaml 五键 → 实例值 (端到端)"""
        import os
        ops_path = os.path.join(os.path.dirname(__file__), "..", "..", "..",
                                "config", "defaults", "ops.yaml")
        with open(ops_path, encoding="utf-8") as f:
            quota = yaml.safe_load(f)["ops"]["agent"]["quota"]
        cfg = _FakeCfg({
            "ops.agent.quota.daily_budget_requests": quota["daily_budget_requests"],
            "ops.agent.quota.daily_budget_cost": quota["daily_budget_cost"],
            "ops.agent.quota.low_remaining_pct": quota["low_remaining_pct"],
            "ops.agent.quota.source_path": quota["source_path"],
            "ops.agent.quota.api_key_hint": quota["api_key_hint"],
        })
        from agent_layer.llm.provider_manager import ProviderManager
        pm = ProviderManager(config=cfg)
        assert pm.quota_manager._daily_budget_requests == quota["daily_budget_requests"]
        assert pm.quota_manager._source_path == quota["source_path"]


class TestWiringGuard:
    """锚2: 接线守卫 — 防再度半程 (configure 生产零调用复发)"""

    def test_ctor_calls_configure(self):
        """源码守卫: ProviderManager.__init__ 必须调 quota_manager.configure"""
        import inspect
        from agent_layer.llm.provider_manager import ProviderManager
        src = inspect.getsource(ProviderManager.__init__)
        assert "quota_manager.configure" in src, (
            "半程接线复发: 构造点未调 configure (默合假象回归)")

    def test_hardcode_has_authority_comment(self):
        """兜底注释锚: _DEFAULT_SOURCE_PATH 须指明权威=ops.yaml"""
        import inspect
        from agent_layer.llm import quota_manager as qm_mod
        src = inspect.getsource(qm_mod)
        assert "权威" in src and "ops.yaml" in src, (
            "兜底缺权威指源注释 (下一个读代码的人会再误判哪边是真源)")
