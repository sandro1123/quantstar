"""Config Layer 装饰器验证测试（CF-04 后更新）"""
import pytest
from unittest.mock import patch, PropertyMock
from config_layer import Config, reset_config
from common.decorators import catch_quantstar_error, with_timeout


@pytest.fixture(autouse=True)
def clean_config():
    reset_config()
    yield
    reset_config()


def test_get_returns_default_on_missing_key():
    cfg = Config()
    assert cfg.get("nonexistent.key", "fallback") == "fallback"


def test_get_does_not_raise_on_missing_key():
    cfg = Config()
    result = cfg.get("missing.key")
    assert result is None


def test_set_no_error_on_any_value():
    cfg = Config()
    cfg.register("typed.key", int, 0)
    # set delegates to registry; no exception for any value
    cfg.set("typed.key", "not_an_int")
    # should not raise


def test_load_returns_none_on_missing_config_dir(tmp_path):
    cfg = Config(profile="nonexistent_profile")
    cfg._config_dir = str(tmp_path / "no_such_dir")
    result = cfg.load()
    assert result is None


def test_decorators_on_io_methods():
    """CF-04: register/instantiate/load 保留装饰器（有 I/O 或导入操作）"""
    assert hasattr(Config.register, "__wrapped__")
    assert hasattr(Config.instantiate, "__wrapped__")
    assert hasattr(Config.load, "__wrapped__")


def test_no_decorators_on_memory_methods():
    """CF-04: get/get_section/set/group_keys/group_dict/get_profile 无装饰器"""
    assert not hasattr(Config.get, "__wrapped__")
    assert not hasattr(Config.get_section, "__wrapped__")
    assert not hasattr(Config.set, "__wrapped__")
    assert not hasattr(Config.group_keys, "__wrapped__")
    assert not hasattr(Config.group_dict, "__wrapped__")
    assert not hasattr(Config.get_profile, "__wrapped__")


def test_get_internal_error_returns_caller_default():
    """CF-04: registry 内部故障 → get 返回调用方 default（而非 None）"""
    cfg = Config()
    cfg.set("some.key", "value")
    # 注入故障：让 registry.get 抛异常
    with patch.object(cfg._registry, "get", side_effect=RuntimeError("injected")):
        result = cfg.get("some.key", "my_default")
        assert result == "my_default"


def test_get_internal_error_returns_none_when_no_default():
    """CF-04: 无 default 时内部异常 → None"""
    cfg = Config()
    with patch.object(cfg._registry, "get", side_effect=RuntimeError("injected")):
        result = cfg.get("some.key")
        assert result is None
