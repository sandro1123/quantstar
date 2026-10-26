"""Tests for data_layer/sources — SourceRegistry + adapters"""
import pytest
from unittest.mock import MagicMock
from data_layer.sources.registry import SourceRegistry


def test_source_registry_register_and_query():
    reg = SourceRegistry()
    src = MagicMock()
    src.name = "mock_source"
    src.capabilities = {"kline"}
    src.is_available.return_value = True
    reg.register(src, priority=10)
    assert reg.is_registered("mock_source")
    sources = reg.get_sources_for("kline")
    assert len(sources) == 1
    assert sources[0].name == "mock_source"


def test_source_registry_priority_order():
    reg = SourceRegistry()
    fast = MagicMock(name="fast")
    fast.name = "fast";
    fast.capabilities = {"kline"};
    fast.is_available.return_value = True
    slow = MagicMock(name="slow")
    slow.name = "slow";
    slow.capabilities = {"kline"};
    slow.is_available.return_value = True
    reg.register(slow, priority=30)
    reg.register(fast, priority=10)
    sources = reg.get_sources_for("kline")
    assert sources[0].name == "fast"


def test_source_registry_unavailable_source_filtered():
    reg = SourceRegistry()
    src = MagicMock()
    src.name = "down"
    src.capabilities = {"kline"}
    src.is_available.return_value = False
    reg.register(src)
    assert reg.get_sources_for("kline") == []


def test_source_registry_get_best_source():
    reg = SourceRegistry()
    src = MagicMock()
    src.name = "best"
    src.capabilities = {"moneyflow"}
    src.is_available.return_value = True
    reg.register(src, priority=5)
    best = reg.get_best_source("moneyflow")
    assert best is not None
    assert best.name == "best"


def test_source_registry_no_match():
    reg = SourceRegistry()
    assert reg.get_sources_for("nonexistent") == []
    assert reg.get_best_source("nonexistent") is None
