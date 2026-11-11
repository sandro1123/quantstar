"""季节性因子: 同月去年收益率"""
from strategy_layer.factors.base import BaseFactor, FactorContext, FactorResult


class SeasonalityFactor(BaseFactor):
    name = "seasonality_same_month"
    description = "同月去年收益率"
    category = "momentum"
    window_size = 365

    def compute(self, ctx: FactorContext) -> FactorResult:
        if len(ctx.klines) < 250:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        closes = [k["close"] if isinstance(k, dict) else k.close for k in ctx.klines]
        target_idx = min(252, len(closes) - 1)
        if closes[-target_idx] == 0:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        score = closes[-1] / closes[-target_idx] - 1
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=score, raw_value=score,
            details={"close_today": closes[-1], "close_1y_ago": closes[-target_idx]},
        )