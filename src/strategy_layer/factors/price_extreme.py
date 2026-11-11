from strategy_layer.factors._utils import extract, rolling_max, rolling_min
from strategy_layer.factors.base import BaseFactor, FactorContext, FactorResult

EPS = 1e-8


class Max5Factor(BaseFactor):
    name = "max5"
    description = "5 日最高价"
    category = "technical"
    window_size = 5

    def compute(self, ctx: FactorContext) -> FactorResult:
        n = len(ctx.klines)
        if n < 5:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        closes = extract(ctx.klines, "close")
        score = rolling_max(closes, 5)[-1]
        return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=score, raw_value=score)


class Min5Factor(BaseFactor):
    name = "min5"
    description = "5 日最低价"
    category = "technical"
    window_size = 5

    def compute(self, ctx: FactorContext) -> FactorResult:
        n = len(ctx.klines)
        if n < 5:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        closes = extract(ctx.klines, "close")
        score = rolling_min(closes, 5)[-1]
        return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=score, raw_value=score)


class Rank5Factor(BaseFactor):
    name = "rank5"
    description = "5 日收盘价百分位"
    category = "technical"
    window_size = 5

    def compute(self, ctx: FactorContext) -> FactorResult:
        n = len(ctx.klines)
        if n < 5:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        closes = extract(ctx.klines, "close")
        rmax = rolling_max(closes, 5)[-1]
        rmin = rolling_min(closes, 5)[-1]
        score = (closes[-1] - rmin) / (rmax - rmin) if (rmax - rmin) > EPS else 0.0
        return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=score, raw_value=score)


class Rank10Factor(BaseFactor):
    name = "rank10"
    description = "10 日收盘价百分位"
    category = "technical"
    window_size = 10

    def compute(self, ctx: FactorContext) -> FactorResult:
        n = len(ctx.klines)
        if n < 10:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        closes = extract(ctx.klines, "close")
        rmax = rolling_max(closes, 10)[-1]
        rmin = rolling_min(closes, 10)[-1]
        score = (closes[-1] - rmin) / (rmax - rmin) if (rmax - rmin) > EPS else 0.0
        return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=score, raw_value=score)