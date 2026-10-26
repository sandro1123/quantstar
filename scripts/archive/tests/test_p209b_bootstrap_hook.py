from __future__ import annotations

import sys

import pytest

from bootstrap import _explicit_feature_enabled, _start_valuation_dual_read


class DictConfig:
    def __init__(self, values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)


def test_explicit_feature_boolean_contract():
    for value in (True, 1, "true", "YES", "on"):
        assert _explicit_feature_enabled(value) is True
    for value in (False, 0, "false", "NO", "off", ""):
        assert _explicit_feature_enabled(value) is False
    with pytest.raises(ValueError, match="invalid explicit boolean"):
        _explicit_feature_enabled("maybe")


def test_disabled_hook_does_not_import_service_module(tmp_path):
    sys.modules.pop("data_layer.valuation_dual_read_service", None)
    service = _start_valuation_dual_read(
        str(tmp_path / "missing-formal.db"),
        DictConfig({"data.valuation_dual_read.enabled": "false"}),
    )
    assert service is None
    assert "data_layer.valuation_dual_read_service" not in sys.modules
    assert list(tmp_path.iterdir()) == []


def test_enabled_invalid_configuration_is_not_swallowed(tmp_path):
    with pytest.raises(ValueError, match="requires shadow_db"):
        _start_valuation_dual_read(
            str(tmp_path / "missing-formal.db"),
            DictConfig({"data.valuation_dual_read.enabled": True}),
        )
