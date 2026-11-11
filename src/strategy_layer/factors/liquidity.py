"""流动性因子: 负平均换手率（非流动性溢价）"""
from strategy_layer.factors.base import BaseFactor, FactorContext, FactorResult


class LiquidityFactor(BaseFactor):
    name = "liquidity_illiquid"
    description = "非流动性溢价（低换手率高分）"
    category = "quality"
    window_size = 65

    def compute(self, ctx: FactorContext) -> FactorResult:
        try:
            klines = ctx.klines
            if len(klines) < 5:
                return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
            turnover_values = []
            for k in klines[-60:]:
                turnover = k["turnover_rate"] if isinstance(k, dict) else getattr(k, "turnover_rate", None)
                if turnover is not None and turnover > 0:
                    turnover_values.append(turnover)
            if not turnover_values:
                return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
            avg_turnover = sum(turnover_values) / len(turnover_values)
            score = -avg_turnover
            return FactorResult(
                code=ctx.code, date=ctx.date, factor_name=self.name,
                score=score, raw_value=avg_turnover,
                details={"avg_turnover": avg_turnover},
            )
        except Exception:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)