"""短期动量因子: 5 日收益率（反转效应）"""
from strategy_layer.factors.base import BaseFactor, FactorContext, FactorResult


class ShortTermMomentumFactor(BaseFactor):
    name = "momentum_5d_reversal"
    description = "5 个交易日收益率（反转效应）"
    category = "momentum"
    window_size = 10

    def compute(self, ctx: FactorContext) -> FactorResult:
        if len(ctx.klines) < 6:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        closes = [k["close"] if isinstance(k, dict) else k.close for k in ctx.klines]
        if closes[-6] == 0:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        score = closes[-1] / closes[-6] - 1
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=score, raw_value=score,
            details={"close_today": closes[-1], "close_5d_ago": closes[-6]},
        )