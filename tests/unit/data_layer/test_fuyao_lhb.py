"""DL-06: fuyao 龙虎榜接入 — 字段归一化与主源路由测试 (mock client, 无网络/无生产库)"""

from unittest.mock import Mock

from data_layer.sources.fuyao_client import FuyaoSourceAdapter

FAKE_ITEMS = [
    {"thscode": "002081.SZ", "ticker": "002081", "name": "金螳螂",
     "net_value": 113176950.97, "buy_value": 349534489.45, "sell_value": 236357538.48},
    {"thscode": "600186.SH", "ticker": "600186", "name": "莲花控股",
     "net_value": -80024350.45, "buy_value": 1009626347.14, "sell_value": 1089650697.59},
    {"thscode": "BAD", "ticker": "", "name": "无代码", "net_value": 1.0},
]

def _adapter_with_fake_client():
    client = Mock()
    client.get_dragon_tiger_list = Mock(return_value=FAKE_ITEMS)
    return FuyaoSourceAdapter(client=client), client

def test_fuyao_capabilities_include_lhb():
    assert "lhb" in FuyaoSourceAdapter().capabilities

def test_fuyao_lhb_normalizes_fields():
    adapter, client = _adapter_with_fake_client()
    rows = adapter.get_data("lhb", {"start_date": "2026-08-17", "end_date": "2026-08-17"})
    # 无 ticker 的条目被过滤
    assert len(rows) == 2
    r0 = rows[0]
    assert r0["code"] == "002081"
    assert r0["buy_in"] == 113176950.97       # net_value → 净买
    assert r0["turnover"] == 349534489.45     # buy_value → 总买
    assert r0["capitalization"] == 236357538.48  # sell_value → 总卖

def test_fuyao_lhb_uses_end_date_param():
    adapter, client = _adapter_with_fake_client()
    adapter.get_data("lhb", {"start_date": "2026-08-17", "end_date": "2026-08-17"})
    client.get_dragon_tiger_list.assert_called_once_with("2026-08-17")

def test_fuyao_lhb_empty_when_no_items():
    client = Mock()
    client.get_dragon_tiger_list = Mock(return_value=[])
    adapter = FuyaoSourceAdapter(client=client)
    assert adapter.get_data("lhb", {"end_date": "2026-08-17"}) == []

def test_fuyao_is_best_lhb_source(monkeypatch):
    """DL-06: 双源均可用时 fuyao lhb type_priority=1 优于 zzshare (全局 35)

    单测环境无 FUYAO_API_KEY, fuyao is_available() 默认 False 会被
    get_sources_for 过滤; 此处 mock 两源可用性, 只验证优先级排序。
    """
    from data_layer import DataLayer
    from data_layer.sources.fuyao_client import FuyaoSourceAdapter
    from data_layer.sources.zzshare_source import ZZShareSourceAdapter
    monkeypatch.setattr(FuyaoSourceAdapter, "is_available", lambda self: True)
    monkeypatch.setattr(ZZShareSourceAdapter, "is_available", lambda self: True)
    dl = DataLayer(Mock())
    sources = [s.name for s in dl._source_registry.get_sources_for("lhb")]
    assert sources, "应有可用 lhb 源"
    assert sources[0] == "fuyao", f"lhb 主源应为 fuyao, 实际 {sources}"
