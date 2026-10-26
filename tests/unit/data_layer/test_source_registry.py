"""Tests for SourceRegistry"""
import pytest
from data_layer.sources.registry import SourceRegistry
from data_layer.sources.protocol import DataSource, DATA_TYPES


class MockSource:
    def __init__(self, name: str, capabilities: set, available: bool = True):
        self._name = name
        self._capabilities = capabilities
        self._available = available

    @property
    def name(self) -> str:
        return self._name

    @property
    def capabilities(self) -> set[str]:
        return self._capabilities

    def is_available(self) -> bool:
        return self._available

    def get_data(self, data_type: str, params: dict):
        return {"source": self._name, "type": data_type}


class TestSourceRegistry:
    def test_register_and_get_all(self):
        registry = SourceRegistry()
        src = MockSource("test", {"kline"})
        registry.register(src)
        assert src in registry.get_all_sources()

    def test_register_with_priority(self):
        registry = SourceRegistry()
        low = MockSource("low_pri", {"kline"})
        high = MockSource("high_pri", {"kline"})
        registry.register(high, priority=10)
        registry.register(low, priority=90)
        best = registry.get_best_source("kline")
        assert best.name == "high_pri"

    def test_unregister_removes_source(self):
        registry = SourceRegistry()
        src = MockSource("test", {"kline"})
        registry.register(src)
        registry.unregister("test")
        assert "test" not in [s.name for s in registry.get_all_sources()]

    def test_get_sources_for_returns_only_available(self):
        registry = SourceRegistry()
        available = MockSource("available", {"kline"})
        unavailable = MockSource("unavailable", {"kline"}, available=False)
        registry.register(available)
        registry.register(unavailable)
        sources = registry.get_sources_for("kline")
        assert len(sources) == 1
        assert sources[0].name == "available"

    def test_get_sources_for_filters_by_capability(self):
        registry = SourceRegistry()
        kline_src = MockSource("kline_src", {"kline"})
        financial_src = MockSource("financial_src", {"financial"})
        registry.register(kline_src)
        registry.register(financial_src)
        sources = registry.get_sources_for("kline")
        assert len(sources) == 1
        assert sources[0].name == "kline_src"

    def test_get_best_source_returns_none_when_none_available(self):
        registry = SourceRegistry()
        src = MockSource("unavailable", {"kline"}, available=False)
        registry.register(src)
        assert registry.get_best_source("kline") is None

    def test_is_registered(self):
        registry = SourceRegistry()
        src = MockSource("test", {"kline"})
        registry.register(src)
        assert registry.is_registered("test") is True
        assert registry.is_registered("nonexistent") is False

    def test_get_available_sources(self):
        registry = SourceRegistry()
        registry.register(MockSource("a", {"kline"}))
        registry.register(MockSource("b", {"kline"}, available=False))
        registry.register(MockSource("c", {"financial"}))
        avail = registry.get_available_sources()
        assert len(avail) == 2
        assert all(s.is_available() for s in avail)

    def test_data_types_constants(self):
        assert "kline" in DATA_TYPES
        assert "valuation" in DATA_TYPES
        assert "financial" in DATA_TYPES
        assert len(DATA_TYPES) >= 14