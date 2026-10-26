"""DL-05 验收测试 — fuyao 能力键名修正 + get_data 未知类型显式报错

F-11: capabilities 声明 "index" 但 get_data 分发键是 index_catalog/index_snapshot,
按 "index" 查必落空; 未知 data_type 原静默返回 None。
修正: capabilities 与分发键一致; 未知类型抛 QuantStarError。
"""
import pytest

from common.errors import QuantStarError
from data_layer.sources.fuyao_client import FuyaoSourceAdapter
from data_layer.sources.registry import SourceRegistry


class _FakeClient:
    """假 client: available()=True, 记录 get_data 调用"""
    def __init__(self):
        self.calls = []

    def available(self):
        return True

    def get_index_catalog(self, tag="industry"):
        self.calls.append(("index_catalog", tag))
        return [{"code": "885001", "name": "银行"}]

    def get_index_snapshot(self, thscodes):
        self.calls.append(("index_snapshot", thscodes))
        return [{"code": "000001.SH", "close": 3000.0}]


def test_capabilities_match_dispatch_keys():
    """capabilities 必须与 get_data 实际分发键一致, 不再含悬空 'index'"""
    adapter = FuyaoSourceAdapter(client=_FakeClient())
    caps = adapter.capabilities
    assert "index" not in caps, "悬空键 'index' 应移除 (由 gtimg 提供)"
    assert "index_catalog" in caps
    assert "index_snapshot" in caps
    # 既有能力保留
    for c in ("financial", "hot_stock", "anomaly", "limit_up", "limit_down", "realtime_quote"):
        assert c in caps


def test_get_data_unknown_type_raises():
    """验收: get_data('index') 抛 QuantStarError (原静默返回 None)"""
    adapter = FuyaoSourceAdapter(client=_FakeClient())
    with pytest.raises(QuantStarError):
        adapter.get_data("index", {})
    with pytest.raises(QuantStarError):
        adapter.get_data("nonexistent_type", {})


def test_get_data_index_catalog_dispatches():
    adapter = FuyaoSourceAdapter(client=_FakeClient())
    result = adapter.get_data("index_catalog", {"tag": "industry"})
    assert result == [{"code": "885001", "name": "银行"}]


def test_get_data_index_snapshot_dispatches():
    adapter = FuyaoSourceAdapter(client=_FakeClient())
    result = adapter.get_data("index_snapshot", {"codes": "000001.SH"})
    assert result == [{"code": "000001.SH", "close": 3000.0}]


def test_registry_routes_index_catalog_to_fuyao():
    """验收: get_sources_for('index_catalog') 能返回 fuyao"""
    registry = SourceRegistry()
    registry.register(FuyaoSourceAdapter(client=_FakeClient()), priority=10)
    sources = registry.get_sources_for("index_catalog")
    assert any(s.name == "fuyao" for s in sources)
    # 'index' 不再路由到 fuyao (悬空键已移除)
    assert not any(s.name == "fuyao" for s in registry.get_sources_for("index"))
