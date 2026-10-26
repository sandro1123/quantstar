"""Tests for config_layer/validator.py"""
import pytest
from config_layer.registry import ConfigRegistry
from config_layer.validator import ConfigValidator


def test_validate_ok():
    reg = ConfigRegistry()
    reg.register("test.key", str, "hello")
    validator = ConfigValidator()
    result = validator.validate(reg)
    assert result.is_valid is True
    assert len(result.errors) == 0


def test_validate_type_mismatch():
    reg = ConfigRegistry()
    reg.register("test.count", int, "not_a_number")
    reg.set("test.count", "not_a_number")
    validator = ConfigValidator()
    result = validator.validate(reg)
    assert result.is_valid is True
    assert len(result.warnings) > 0
    assert any("类型应为" in w and "int" in w for w in result.warnings)


def test_validate_constraints_min():
    reg = ConfigRegistry()
    reg.register("test.ratio", float, 0.5, constraints={"min": 0.0, "max": 1.0})
    reg.set("test.ratio", -0.1)
    validator = ConfigValidator()
    result = validator.validate(reg)
    assert any("-0.1 < min 0.0" in w for w in result.warnings)


def test_validate_constraints_max():
    reg = ConfigRegistry()
    reg.register("test.ratio", float, 0.5, constraints={"min": 0.0, "max": 1.0})
    reg.set("test.ratio", 1.5)
    validator = ConfigValidator()
    result = validator.validate(reg)
    assert any("1.5 > max 1.0" in w for w in result.warnings)


def test_validate_constraints_options():
    reg = ConfigRegistry()
    reg.register("test.mode", str, "auto", constraints={"options": ["auto", "manual"]})
    reg.set("test.mode", "invalid")
    validator = ConfigValidator()
    result = validator.validate(reg)
    assert any("invalid not in" in w for w in result.warnings)


def test_validate_constraints_min_length():
    reg = ConfigRegistry()
    reg.register("test.name", str, "default", constraints={"min_length": 3})
    reg.set("test.name", "ab")
    validator = ConfigValidator()
    result = validator.validate(reg)
    assert any("length 2 < min_length 3" in w for w in result.warnings)


def test_validate_constraints_regex():
    reg = ConfigRegistry()
    reg.register("test.email", str, "a@b.com", constraints={"regex": r".+@.+\..+"})
    reg.set("test.email", "invalid")
    validator = ConfigValidator()
    result = validator.validate(reg)
    assert any("does not match regex" in w for w in result.warnings)


def test_validate_constraints_ok():
    reg = ConfigRegistry()
    reg.register("test.ratio", float, 0.5, constraints={"min": 0.0, "max": 1.0})
    validator = ConfigValidator()
    result = validator.validate(reg)
    assert len(result.warnings) == 0