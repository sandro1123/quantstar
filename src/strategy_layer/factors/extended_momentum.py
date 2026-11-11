"""扩展动量因子: 60/120/250日累计收益率 + 相对强度 + 价格加速度"""
from strategy_layer.factors._utils import extract
from strategy_layer.factors.base import BaseFactor, FactorContext, FactorResult


class Momentum60Factor(BaseFactor):
    name = "momentum_60d"
    description = "60个交易日累计收益率"
    category = "momentum"
    window_size = 65

    def compute(self, ctx: FactorContext) -> FactorResult:
        if len(ctx.klines) < 60:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        closes = extract(ctx.klines, "close")
        if closes[-60] == 0:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        score = closes[-1] / closes[-60] - 1
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=score, raw_value=score,
            details={"close_today": closes[-1], "close_60d_ago": closes[-60]},
        )


class Momentum120Factor(BaseFactor):
    name = "momentum_120d"
    description = "120个交易日累计收益率"
    category = "momentum"
    window_size = 125

    def compute(self, ctx: FactorContext) -> FactorResult:
        if len(ctx.klines) < 120:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        closes = extract(ctx.klines, "close")
        if closes[-120] == 0:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        score = closes[-1] / closes[-120] - 1
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=score, raw_value=score,
            details={"close_today": closes[-1], "close_120d_ago": closes[-120]},
        )


class Momentum250Factor(BaseFactor):
    name = "momentum_250d"
    description = "250个交易日累计收益率"
    category = "momentum"
    window_size = 255

    def compute(self, ctx: FactorContext) -> FactorResult:
        if len(ctx.klines) < 250:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        closes = extract(ctx.klines, "close")
        if closes[-250] == 0:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        score = closes[-1] / closes[-250] - 1
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=score, raw_value=score,
            details={"close_today": closes[-1], "close_250d_ago": closes[-250]},
        )


class RelativeStrengthFactor(BaseFactor):
    name = "relative_strength"
    description = "相对强度(vs等权市场)"
    category = "momentum"
    window_size = 65

    def compute(self, ctx: FactorContext) -> FactorResult:
        if len(ctx.klines) < 20:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        closes = extract(ctx.klines, "close")
        if closes[-20] == 0:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        score = closes[-1] / closes[-20] - 1
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=score, raw_value=score,
            details={"stock_20d_return": score},
        )


class PriceAccelerationFactor(BaseFactor):
    name = "price_acceleration"
    description = "价格加速度(动量的动量)"
    category = "momentum"
    window_size = 30

    def compute(self, ctx: FactorContext) -> FactorResult:
        if len(ctx.klines) < 21:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        closes = extract(ctx.klines, "close")
        if closes[-11] == 0 or closes[-21] == 0:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        mom_recent = closes[-1] / closes[-11] - 1
        mom_prev = closes[-11] / closes[-21] - 1
        score = mom_recent - mom_prev
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=score, raw_value=score,
            details={"mom_recent_10d": mom_recent, "mom_prev_10d": mom_prev},
        )
