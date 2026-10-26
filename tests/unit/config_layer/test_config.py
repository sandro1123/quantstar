"""Config 类单元测试"""
import pytest
from config_layer import Config, get_config, reset_config


@pytest.fixture(autouse=True)
def clean_config():
    reset_config()
    yield
    reset_config()


def test_register_and_get():
    cfg = Config()
    cfg.register("test.key", str, "hello")
    assert cfg.get("test.key") == "hello"


def test_set_and_get():
    cfg = Config()
    cfg.register("test.val", int, 0)
    cfg.set("test.val", 42)
    assert cfg.get("test.val") == 42


def test_get_missing_returns_default():
    cfg = Config()
    assert cfg.get("nonexistent", "fallback") == "fallback"


def test_to_dict_hides_secrets():
    cfg = Config()
    cfg.register("public", str, "visible")
    cfg.register("private", str, "hidden", secret=True)
    d = cfg.to_dict()
    assert d["public"] == "visible"
    assert d["private"] == "******"


def test_to_dict_show_secrets():
    cfg = Config()
    cfg.register("private", str, "hidden", secret=True)
    d = cfg.to_dict(show_secrets=True)
    assert d["private"] == "hidden"


def test_get_section_reconstructs_nested():
    cfg = Config()
    cfg.set("ops.pipelines.morning_brief.schedule", "08:00")
    cfg.set("ops.pipelines.morning_brief.days", "mon-fri")
    cfg.set("ops.pipelines.evening_report.schedule", "18:00")
    section = cfg.get_section("ops.pipelines")
    assert "morning_brief" in section
    assert section["morning_brief"]["schedule"] == "08:00"
    assert section["evening_report"]["schedule"] == "18:00"


def test_on_change_callback():
    cfg = Config()
    cfg.register("watch.me", str, "old")
    results = []
    cfg.on_change("watch.me", lambda v: results.append(v))
    cfg.set("watch.me", "new")
    assert results == ["new"]


def test_get_profile():
    cfg = Config(profile="prod")
    assert cfg.get_profile() == "prod"


def test_type_cast_int():
    cfg = Config()
    cfg.register("num", int, 0)
    cfg.set("num", "99")
    assert cfg.get("num") == 99
    assert isinstance(cfg.get("num"), int)


def test_type_cast_bool():
    cfg = Config()
    cfg.register("flag", bool, False)
    cfg.set("flag", "true")
    assert cfg.get("flag") is True


def test_auto_register_unknown_key():
    cfg = Config()
    cfg.set("dynamic.key", "value")
    assert cfg.get("dynamic.key") == "value"


def test_group_keys():
    cfg = Config()
    cfg.set("ml.model.xgboost.lr", 0.01)
    cfg.set("ml.model.xgboost.max_depth", 6)
    cfg.set("ml.model.lightgbm.lr", 0.02)
    keys = cfg.group_keys("ml.model.xgboost")
    assert "lr" in keys
    assert "max_depth" in keys


def test_group_dict():
    cfg = Config()
    cfg.set("data.source.fuyao.api_key", "abc")
    cfg.set("data.source.fuyao.timeout", 30)
    d = cfg.group_dict("data.source.fuyao")
    assert d["api_key"] == "abc"
    assert d["timeout"] == 30


def test_instantiate():
    from dataclasses import dataclass

    @dataclass
    class Dummy:
        x: int = 0
        y: str = ""

    import sys
    sys.modules["_test_dummy"] = type(sys)("_test_dummy")
    sys.modules["_test_dummy"].Dummy = Dummy
    cfg = Config()
    cfg.set("test.target._target_", "_test_dummy.Dummy")
    cfg.set("test.target.x", 42)
    cfg.set("test.target.y", "hello")
    obj = cfg.instantiate("test.target")
    assert obj.x == 42
    assert obj.y == "hello"


def test_instantiate_with_extra_kwargs():
    cfg = Config()
    cfg.set("test.target._target_", "builtins.dict")
    cfg.set("test.target.a", 1)
    obj = cfg.instantiate("test.target", b=2)
    assert obj == {"a": 1, "b": 2}


def test_instantiate_missing_target():
    cfg = Config()
    cfg.set("test.no_target.x", 1)
    assert cfg.instantiate("test.no_target") is None


def test_instantiate_bad_format():
    cfg = Config()
    cfg.set("test.bad._target_", "InvalidNoDot")
    assert cfg.instantiate("test.bad") is None


def test_instantiate_import_error():
    cfg = Config()
    cfg.set("test.missing._target_", "nonexistent_module.Class")
    assert cfg.instantiate("test.missing") is None


def test_health_before_load():
    cfg = Config(profile="test")
    result = cfg.health()
    assert result.layer == "config"
    assert result.status == "red"
    assert len(result.checks) == 3
    assert result.checks[0].name == "config_loaded"
    assert result.checks[0].status == "warn"


def test_health_after_load():
    cfg = Config(profile="test")
    cfg._loaded = True
    cfg.set("data.db_path", "/tmp/test.db")
    cfg.set("data.sources", {"fuyao": {}})
    cfg.set("strategy.factors", ["factor_a", "factor_b"])
    result = cfg.health()
    assert result.layer == "config"
    assert result.status == "green"
    assert result.checks[0].status == "pass"
