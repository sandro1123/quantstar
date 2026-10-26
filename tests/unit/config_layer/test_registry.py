"""Tests for config_layer/registry.py"""
import pytest
from config_layer.registry import ConfigRegistry, ConfigEntry


def test_register_and_get():
    reg = ConfigRegistry()
    reg.register("test.key", str, "hello", description="测试配置")
    assert reg.get("test.key") == "hello"


def test_get_type_cast():
    reg = ConfigRegistry()
    reg.register("test.count", int, "42")
    assert reg.get("test.count") == 42
    assert isinstance(reg.get("test.count"), int)


def test_get_missing_key():
    reg = ConfigRegistry()
    assert reg.get("nonexistent") is None


def test_get_typed_default():
    reg = ConfigRegistry()
    reg.register("test.ratio", float, 0.08)
    assert reg.get("test.ratio") == 0.08


def test_to_dict():
    reg = ConfigRegistry()
    reg.register("key1", str, "val1")
    reg.register("key2", int, 42, secret=True)
    d = reg.to_dict()
    assert d["key1"] == "val1"
    assert d["key2"] == "******"


def test_env_var_mapping():
    reg = ConfigRegistry()
    reg.register("db.host", str, "localhost", env_var="DB_HOST")
    entry = reg._entries["db.host"]
    assert entry.env_var == "DB_HOST"