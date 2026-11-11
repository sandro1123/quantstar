"""市值因子: 负市值（小市值溢价）"""
from strategy_layer.factors.base import BaseFactor, FactorContext, FactorResult, is_valid_positive


class SizeFactor(BaseFactor):
    name = "size_small_cap"
    description = "小市值溢价（负市值）"
    category = "value"
    window_size = 1

    def compute(self, ctx: FactorContext) -> FactorResult:
        missing = FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=None, raw_value=None)
        if not ctx.valuations:
            return missing
        v = ctx.valuations[-1]
        market_cap = v.get("market_cap") if isinstance(v, dict) else getattr(v, "market_cap", None)
        if not is_valid_positive(market_cap):
            return FactorResult(
                code=ctx.code, date=ctx.date, factor_name=self.name,
                score=None, raw_value=None,
                details={"market_cap": market_cap},
            )
        score = -market_cap
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=score, raw_value=market_cap,
            details={"market_cap": market_cap},
        )