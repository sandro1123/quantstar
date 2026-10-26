"""Tests for data layer sources and remaining modules"""
from data_layer.sources.registry import SourceRegistry
from data_layer.sources.protocol import DATA_TYPES


class MockSource:
    def __init__(self, name, caps, available=True):
        self._name = name
        self._caps = caps
        self._avail = available
    @property
    def name(self): return self._name
    @property
    def capabilities(self): return self._caps
    def is_available(self): return self._avail
    def get_data(self, dtype, params): return {}


class TestSourceRegistry:
    def test_register_priority(self):
        r = SourceRegistry()
        r.register(MockSource("a", {"kline"}), priority=10)
        r.register(MockSource("b", {"kline"}), priority=90)
        best = r.get_best_source("kline")
        assert best.name == "a"

    def test_unregister(self):
        r = SourceRegistry()
        r.register(MockSource("a", {"kline"}))
        r.unregister("a")
        assert r.is_registered("a") is False

    def test_get_sources_filters_by_availability(self):
        r = SourceRegistry()
        r.register(MockSource("a", {"kline"}, available=True))
        r.register(MockSource("b", {"kline"}, available=False))
        assert len(r.get_sources_for("kline")) == 1

    def test_get_best_source_returns_none(self):
        r = SourceRegistry()
        assert r.get_best_source("nonexistent") is None

    def test_data_types_defined(self):
        assert "kline" in DATA_TYPES
        assert "financial" in DATA_TYPES