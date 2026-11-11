"""质量因子: ROE + 毛利率"""
from strategy_layer.factors.base import BaseFactor, FactorContext, FactorResult


class QualityFactor(BaseFactor):
    name = "roe_quality"
    description = "ROE 质量因子"
    category = "quality"
    window_size = 1

    def compute(self, ctx: FactorContext) -> FactorResult:
        if not ctx.financials:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        f = ctx.financials[-1]
        roe = f["roe"] if isinstance(f, dict) else f.roe
        gross = f["gross_margin"] if isinstance(f, dict) else f.gross_margin
        score = roe * 0.7 + gross * 0.3
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=score, raw_value=roe,
            details={"roe": roe, "gross_margin": gross},
        )
