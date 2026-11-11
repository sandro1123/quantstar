from strategy_layer.factors._utils import extract, rolling_mean
from strategy_layer.factors.base import BaseFactor, FactorContext, FactorResult

EPS = 1e-8


class Ma5DevFactor(BaseFactor):
    name = "ma5_dev"
    description = "(close - ma5) / |ma5|"
    category = "technical"
    window_size = 5

    def compute(self, ctx: FactorContext) -> FactorResult:
        n = len(ctx.klines)
        if n < 5:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        closes = extract(ctx.klines, "close")
        ma5 = rolling_mean(closes, 5)
        denom = max(EPS, abs(ma5[-1]))
        score = (closes[-1] - ma5[-1]) / denom
        return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=score, raw_value=score)


class Ma10DevFactor(BaseFactor):
    name = "ma10_dev"
    description = "(close - ma10) / |ma10|"
    category = "technical"
    window_size = 10

    def compute(self, ctx: FactorContext) -> FactorResult:
        n = len(ctx.klines)
        if n < 10:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        closes = extract(ctx.klines, "close")
        ma10 = rolling_mean(closes, 10)
        denom = max(EPS, abs(ma10[-1]))
        score = (closes[-1] - ma10[-1]) / denom
        return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=score, raw_value=score)