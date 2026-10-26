"""RegimeSplit 单元测试"""
import pytest
from backtest_layer.validation.regime_split import RegimeSplit


def test_regime_split_basic():
    rs = RegimeSplit()
    result = rs.split([100, 110, 105, 95, 90], ["bull", "bull", "neutral", "bear", "bear"])
    assert "bull" in result
    assert "bear" in result
    assert result["bull"]["return"] == pytest.approx(0.1, abs=0.01)
    assert result["bear"]["return"] < 0
