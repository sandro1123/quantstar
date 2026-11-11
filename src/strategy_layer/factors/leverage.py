"""杠杆因子: 负资产负债率"""
from strategy_layer.factors.base import BaseFactor, FactorContext, FactorResult


class LeverageFactor(BaseFactor):
    name = "leverage_low"
    description = "低杠杆（负资产负债率）"
    category = "quality"
    window_size = 1

    def compute(self, ctx: FactorContext) -> FactorResult:
        if not ctx.financials:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        f = ctx.financials[-1]
        total_assets = f["total_assets"] if isinstance(f, dict) else getattr(f, "total_assets", 0)
        total_liabilities = f["total_liabilities"] if isinstance(f, dict) else getattr(f, "total_liabilities", 0)
        if total_assets <= 0:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        ratio = total_liabilities / total_assets
        score = -ratio
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=score, raw_value=ratio,
            details={"debt_to_assets": ratio},
        )