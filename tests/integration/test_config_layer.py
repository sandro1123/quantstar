"""Integration tests for config_layer"""
import pytest
from config_layer import get_config, reset_config


def test_config_singleton():
    c1 = get_config()
    c2 = get_config()
    assert c1 is c2


def test_config_load_profile(monkeypatch):
    # TH-01: 模块级 os.environ 赋值会污染后续测试 (integration 按字母序先于
    # unit 收集, 曾掩盖 CF-01 prod 校验) — 改 monkeypatch 自动还原。
    monkeypatch.setenv("CONFIG_ENV", "dev")
    cfg = get_config()
    cfg.load()
    assert cfg.get_profile() == "dev"


def test_config_get_registered():
    cfg = get_config()
    cfg.register("test.key", str, "hello")
    assert cfg.get("test.key") == "hello"


def test_config_get_nonexistent():
    cfg = get_config()
    assert cfg.get("nothing.here") is None