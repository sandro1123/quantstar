"""行业动量因子: 概念板块平均收益率"""
from strategy_layer.factors.base import BaseFactor, FactorContext, FactorResult


class IndustryMomentumFactor(BaseFactor):
    name = "industry_momentum"
    description = "概念板块平均收益率 (基于概念change_pct)"
    category = "momentum"
    window_size = 1

    def compute(self, ctx: FactorContext) -> FactorResult:
        if not ctx.concept_data:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=0.0, raw_value=0.0)

        changes = [c.get("change_pct", 0.0) for c in ctx.concept_data if isinstance(c, dict)]
        if not changes:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=0.0, raw_value=0.0)

        avg_change = sum(changes) / len(changes)
        return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=avg_change, raw_value=avg_change)
