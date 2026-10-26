"""DL-31: fuyao 三张财务报表暴露为注册表 capabilities。

验收:
1. capabilities 集合含 income_statement/balance_sheet/cash_flow (差集为空)
2. get_data 分发键与 capabilities 一致 (不 duck-type)
3. type_priorities 注册 (源能进降级链)

红-绿: 撤回 DL-31 补丁时本文件必须失败。
"""
import pytest

from data_layer.sources.fuyao_client import FuyaoSourceAdapter


def test_capabilities_include_three_financial_statements():
    adapter = FuyaoSourceAdapter()
    caps = adapter.capabilities
    for cap in ("income_statement", "balance_sheet", "cash_flow"):
        assert cap in caps, f"capabilities 缺 {cap}"


def test_get_data_dispatches_financial_statements():
    """get_data 必须能分发三个新 data_type (不抛 QuantStarError)。"""
    from unittest.mock import MagicMock, patch

    adapter = FuyaoSourceAdapter()
    fake_client = MagicMock()
    fake_client.get_income_statements.return_value = [{"net_profit": 100}]
    fake_client.get_balance_sheets.return_value = [{"assets_total": 200}]
    fake_client.get_cash_flow.return_value = [{"act_cash_flow_net": 300}]
    adapter._client = fake_client

    assert adapter.get_data("income_statement", {"code": "600519.SH"}) == [{"net_profit": 100}]
    assert adapter.get_data("balance_sheet", {"code": "600519.SH"}) == [{"assets_total": 200}]
    assert adapter.get_data("cash_flow", {"code": "600519.SH"}) == [{"act_cash_flow_net": 300}]


def test_get_data_passes_period_and_limit():
    from unittest.mock import MagicMock

    adapter = FuyaoSourceAdapter()
    fake_client = MagicMock()
    fake_client.get_income_statements.return_value = []
    adapter._client = fake_client

    adapter.get_data("income_statement", {"code": "600519.SH", "period": "annual", "limit": 8})
    fake_client.get_income_statements.assert_called_once_with(
        "600519.SH", period="annual", limit=8)


def test_registry_resolves_financial_capabilities():
    """注册表按 capabilities 过滤能找到 fuyao (会话C 踩坑防复发:
    type_priorities 注册了但 capabilities 没扩 → 源永不进降级链)。"""
    from data_layer.sources.registry import SourceRegistry

    reg = SourceRegistry()
    adapter = FuyaoSourceAdapter()
    # mock is_available 避免真实网络探测
    adapter.is_available = lambda: True
    reg.register(adapter, priority=10,
                 type_priorities={"income_statement": 1, "balance_sheet": 1, "cash_flow": 1})

    for cap in ("income_statement", "balance_sheet", "cash_flow"):
        sources = reg.get_sources_for(cap)
        assert sources, f"注册表按 {cap} 查不到源 (capabilities/type_priorities 不对齐)"
        assert reg.get_best_source(cap) is adapter
