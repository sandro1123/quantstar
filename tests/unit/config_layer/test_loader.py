"""Tests for config_layer/loader.py"""
import tempfile
import os
import yaml
import pytest
from config_layer.registry import ConfigRegistry
from config_layer.loader import ConfigLoader


def test_load_single_yaml():
    reg = ConfigRegistry()
    reg.register("test.key", str, "default")
    loader = ConfigLoader()
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump({"test": {"key": "override"}}, f)
        f.flush()
        loader.load_file(reg, f.name)
    os.unlink(f.name)
    assert reg.get("test.key") == "override"


def test_load_multiple_merge():
    reg = ConfigRegistry()
    reg.register("key1", str, "default1")
    reg.register("key2", str, "default2")
    loader = ConfigLoader()
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f1:
        yaml.dump({"key1": "from_file1"}, f1)
        f1.flush()
        loader.load_file(reg, f1.name)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f2:
        yaml.dump({"key2": "from_file2"}, f2)
        f2.flush()
        loader.load_file(reg, f2.name)
    os.unlink(f1.name)
    os.unlink(f2.name)
    assert reg.get("key1") == "from_file1"
    assert reg.get("key2") == "from_file2"


def test_variable_interpolation():
    reg = ConfigRegistry()
    reg.register("paths.data", str, "/data")
    reg.register("paths.logs", str, "${paths.data}/logs")
    loader = ConfigLoader()
    loader._interpolate(reg)
    assert reg.get("paths.logs") == "/data/logs"