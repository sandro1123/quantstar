"""Tests for Config Layer factory and utility functions"""
import pytest
from config_layer import Config
from config_layer.registry import ConfigEntry, ConfigRegistry


class FakeRegistry:
    def __init__(self, data=None):
        self._entries = {}
        if data:
            for k, v in data.items():
                entry = ConfigEntry(key=k, type_=type(v))
                entry._value = v
                entry.source = "test"
                self._entries[k] = entry

    def get(self, key, default=None):
        entry = self._entries.get(key)
        return entry.value if entry else default

    def to_dict(self, show_secrets=False):
        return {k: v.value for k, v in self._entries.items()}


class TestConfigInstantiate:
    def test_instantiate_builtin_class(self):
        config = Config()
        config._registry = FakeRegistry({
            "test._target_": "collections.OrderedDict",
        })
        config._loaded = True
        result = config.instantiate("test")
        from collections import OrderedDict
        assert isinstance(result, OrderedDict)

    def test_instantiate_with_kwargs(self):
        config = Config()
        config._registry = FakeRegistry({
            "test._target_": "collections.OrderedDict",
        })
        config._loaded = True
        result = config.instantiate("test", a=1)
        assert isinstance(result, dict)
        assert result.get("a") == 1

    def test_instantiate_returns_none_on_missing_target(self):
        config = Config()
        config._registry = FakeRegistry({})
        config._loaded = True
        result = config.instantiate("test")
        assert result is None

    def test_instantiate_returns_none_on_bad_module(self):
        config = Config()
        config._registry = FakeRegistry({
            "test._target_": "nonexistent_module.SomeClass",
        })
        config._loaded = True
        result = config.instantiate("test")
        assert result is None

    def test_instantiate_returns_none_on_bad_class(self):
        config = Config()
        config._registry = FakeRegistry({
            "test._target_": "collections.NonexistentClass",
        })
        config._loaded = True
        result = config.instantiate("test")
        assert result is None


class TestConfigGroupKeys:
    def test_group_keys(self):
        config = Config()
        config._registry = FakeRegistry({
            "data_source.kline.type": "daily",
            "data_source.kline.limit": 100,
            "data_source.valuation.type": "pe",
        })
        config._loaded = True
        keys = config.group_keys("data_source")
        assert "kline.type" in keys
        assert "kline.limit" in keys
        assert "valuation.type" in keys


class TestConfigGroupDict:
    def test_group_dict(self):
        config = Config()
        config._registry = FakeRegistry({
            "data_source.kline.type": "daily",
            "data_source.kline.limit": 100,
        })
        config._loaded = True
        d = config.group_dict("data_source.kline")
        assert d.get("type") == "daily"
        assert d.get("limit") == 100

    def test_group_dict_strips_prefix(self):
        config = Config()
        config._registry = FakeRegistry({
            "a.b.c": "1",
            "a.b.d": "2",
        })
        config._loaded = True
        d = config.group_dict("a.b")
        assert "c" in d
        assert "d" in d