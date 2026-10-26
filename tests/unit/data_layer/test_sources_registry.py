"""测试 SourceRegistry"""

import pytest
from data_layer.sources.registry import SourceRegistry


class MockSource:
    def __init__(self, name: str, caps: set[str], available: bool = True):
        self._name = name
        self._caps = caps
        self._available = available

    @property
    def name(self) -> str:
        return self._name

    @property
    def capabilities(self) -> set[str]:
        return self._caps

    def is_available(self) -> bool:
        return self._available

    def get_data(self, data_type: str, params: dict):
        return {"source": self._name, "type": data_type}


def test_register_source():
    """测试注册源"""
    registry = SourceRegistry()
    source = MockSource("test", {"kline"})
    registry.register(source)
    assert registry.is_registered("test")


def test_unregister_source():
    """测试注销源"""
    registry = SourceRegistry()
    source = MockSource("test", {"kline"})
    registry.register(source)
    registry.unregister("test")
    assert not registry.is_registered("test")


def test_unregister_nonexistent():
    """测试注销不存在的源（不报错）"""
    registry = SourceRegistry()
    registry.unregister("nonexistent")


def test_get_sources_for():
    """测试获取数据类型对应的源"""
    registry = SourceRegistry()
    registry.register(MockSource("source1", {"kline", "valuation"}))
    registry.register(MockSource("source2", {"kline"}))

    sources = registry.get_sources_for("kline")
    assert len(sources) == 2


def test_get_best_source():
    """测试获取最佳源"""
    registry = SourceRegistry()
    registry.register(MockSource("slow", {"kline"}), priority=10)
    registry.register(MockSource("fast", {"kline"}), priority=1)

    best = registry.get_best_source("kline")
    assert best.name == "fast"


def test_get_best_source_unavailable():
    """测试获取不可用源"""
    registry = SourceRegistry()
    registry.register(MockSource("unavailable", {"kline"}, available=False))

    best = registry.get_best_source("kline")
    assert best is None


def test_get_best_source_no_match():
    """测试无匹配数据类型时返回 None"""
    registry = SourceRegistry()
    registry.register(MockSource("source1", {"kline"}))

    best = registry.get_best_source("valuation")
    assert best is None


def test_get_sources_sorted_by_priority():
    """测试按优先级排序"""
    registry = SourceRegistry()
    registry.register(MockSource("low", {"kline"}), priority=10)
    registry.register(MockSource("high", {"kline"}), priority=1)
    registry.register(MockSource("mid", {"kline"}), priority=5)

    sources = registry.get_sources_for("kline")
    assert [s.name for s in sources] == ["high", "mid", "low"]


def test_get_sources_excludes_unavailable():
    """测试排除不可用源"""
    registry = SourceRegistry()
    registry.register(MockSource("available", {"kline"}), priority=1)
    registry.register(MockSource("unavailable", {"kline"}, available=False), priority=2)

    sources = registry.get_sources_for("kline")
    assert len(sources) == 1
    assert sources[0].name == "available"


def test_get_all_sources():
    """测试获取所有已注册的源"""
    registry = SourceRegistry()
    registry.register(MockSource("a", {"kline"}))
    registry.register(MockSource("b", {"valuation"}))

    all_sources = registry.get_all_sources()
    assert len(all_sources) == 2


def test_get_available_sources():
    """测试获取所有可用的源"""
    registry = SourceRegistry()
    registry.register(MockSource("available", {"kline"}))
    registry.register(MockSource("unavailable", {"kline"}, available=False))

    available = registry.get_available_sources()
    assert len(available) == 1
    assert available[0].name == "available"


def test_reregister_source():
    """测试重新注册源会覆盖旧的"""
    registry = SourceRegistry()
    registry.register(MockSource("test", {"kline"}), priority=10)
    registry.register(MockSource("test", {"kline"}), priority=1)

    sources = registry.get_sources_for("kline")
    assert len(sources) == 1


def test_type_priority_overrides_global():
    """DL-02: type_priorities 覆盖全局优先级"""
    registry = SourceRegistry()
    # fuyao global=10 but limit_up type_priority=1
    registry.register(MockSource("fuyao", {"limit_up", "financial"}), priority=10,
                      type_priorities={"limit_up": 1})
    # akshare global=30 but limit_up type_priority=2
    registry.register(MockSource("akshare", {"limit_up"}), priority=30,
                      type_priorities={"limit_up": 2})
    # tushare global=20, no type override for limit_up
    registry.register(MockSource("tushare", {"limit_up"}), priority=20)

    sources = registry.get_sources_for("limit_up")
    assert [s.name for s in sources] == ["fuyao", "akshare", "tushare"]

    # for financial, fuyao uses global priority=10, tushare not in caps
    registry.register(MockSource("mootdx", {"financial"}), priority=3)
    sources = registry.get_sources_for("financial")
    assert [s.name for s in sources] == ["mootdx", "fuyao"]


def test_type_priority_fallback_to_global():
    """DL-02: 无 type_priorities 时用全局 priority"""
    registry = SourceRegistry()
    registry.register(MockSource("a", {"kline"}), priority=5)
    registry.register(MockSource("b", {"kline"}), priority=1)

    sources = registry.get_sources_for("kline")
    assert [s.name for s in sources] == ["b", "a"]
