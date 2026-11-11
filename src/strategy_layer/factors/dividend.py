"""股息因子: 股息率"""
from strategy_layer.factors.base import BaseFactor, FactorContext, FactorResult, is_finite_number


class DividendFactor(BaseFactor):
    name = "dividend_yield"
    description = "股息率"
    category = "value"
    window_size = 1

    def compute(self, ctx: FactorContext) -> FactorResult:
        try:
            if not ctx.valuations:
                return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=None, raw_value=None)
            v = ctx.valuations[-1]
            dy = v.get("dividend_yield") if isinstance(v, dict) else getattr(v, "dividend_yield", None)
            if not is_finite_number(dy):
                return FactorResult(
                    code=ctx.code, date=ctx.date, factor_name=self.name,
                    score=None, raw_value=None,
                    details={"dividend_yield": dy},
                )
            return FactorResult(
                code=ctx.code, date=ctx.date, factor_name=self.name,
                score=dy, raw_value=dy,
                details={"dividend_yield": dy},
            )
        except Exception:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=None, raw_value=None)