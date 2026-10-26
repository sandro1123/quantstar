"""Tests for Strategy Layer factor base classes"""
import pytest
from strategy_layer.factors.base import BaseFactor, FactorContext, FactorResult


class TestFactorContext:
    def test_default_values(self):
        ctx = FactorContext(code="000001", date="2026-07-24")
        assert ctx.code == "000001"
        assert ctx.date == "2026-07-24"
        assert ctx.klines == []
        assert ctx.financials == []
        assert ctx.valuations == []
        assert ctx.sector == ""

    def test_with_klines(self):
        ctx = FactorContext(code="000001", date="2026-07-24",
                            klines=[{"close": 10.0}, {"close": 11.0}])
        assert len(ctx.klines) == 2


class TestFactorResult:
    def test_default_values(self):
        r = FactorResult(code="000001", date="2026-07-24", factor_name="test")
        # #122 案A: 默认 0.0→None — bare 守卫 return 不得吃假 0.0
        assert r.score is None
        assert r.raw_value is None
        assert r.details == {}

    def test_with_values(self):
        r = FactorResult(code="000001", date="2026-07-24", factor_name="test",
                        score=1.5, raw_value=100.0, details={"key": "val"})
        assert r.score == 1.5
        assert r.raw_value == 100.0
        assert r.details["key"] == "val"


class TestBaseFactor:
    def test_concrete_factor_computes(self):
        class SimpleFactor(BaseFactor):
            name = "simple"
            description = "Simple test factor"
            category = "momentum"
            window_size = 5

            def compute(self, ctx):
                close = ctx.klines[-1]["close"] if ctx.klines else 0
                return FactorResult(
                    code=ctx.code, date=ctx.date, factor_name=self.name,
                    score=close, raw_value=close,
                )

        f = SimpleFactor()
        assert f.name == "simple"
        assert f.category == "momentum"
        assert f.window_size == 5

        ctx = FactorContext(code="000001", date="2026-07-24",
                           klines=[{"close": 10.5}, {"close": 11.2}])
        result = f.compute(ctx)
        assert result.score == 11.2

    def test_factor_requires_implementation(self):
        with pytest.raises(TypeError):
            BaseFactor()

    def test_factor_metadata(self):
        class TestFactor(BaseFactor):
            name = "test"
            description = "A test factor"
            category = "value"

            def compute(self, ctx):
                return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)

        f = TestFactor()
        assert f.name == "test"
        assert f.description == "A test factor"
        assert f.category == "value"