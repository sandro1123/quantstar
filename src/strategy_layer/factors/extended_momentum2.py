from strategy_layer.factors._utils import extract
from strategy_layer.factors.base import BaseFactor, FactorContext, FactorResult


class Momentum480Factor(BaseFactor):
    name = "momentum_480d"
    description = "480个交易日累计收益率"
    category = "momentum"
    window_size = 485

    def compute(self, ctx: FactorContext) -> FactorResult:
        closes = extract(ctx.klines, "close")
        if len(closes) < 480:
            return FactorResult(score=0.0)
        score = closes[-1] / closes[-480] - 1
        return FactorResult(score=score)


class MomentumReversalFactor(BaseFactor):
    name = "momentum_reversal_1d"
    description = "短期反转(1日)"
    category = "momentum"
    window_size = 5

    def compute(self, ctx: FactorContext) -> FactorResult:
        closes = extract(ctx.klines, "close")
        if len(closes) < 2:
            return FactorResult(score=0.0)
        ret = closes[-1] / closes[-2] - 1
        score = -ret
        return FactorResult(score=score)


class SeasonalMomentumFactor(BaseFactor):
    name = "seasonal_momentum"
    description = "同月动量(去年同期至今)"
    category = "momentum"
    window_size = 260

    def compute(self, ctx: FactorContext) -> FactorResult:
        closes = extract(ctx.klines, "close")
        if len(closes) < 252:
            return FactorResult(score=0.0)
        score = closes[-1] / closes[-252] - 1
        return FactorResult(score=score)


class CrossSectionalMomentumFactor(BaseFactor):
    name = "cs_momentum"
    description = "截面动量排名"
    category = "momentum"
    window_size = 25

    def compute(self, ctx: FactorContext) -> FactorResult:
        closes = extract(ctx.klines, "close")
        if len(closes) < 21:
            return FactorResult(score=0.0)
        ret_20d = closes[-1] / closes[-21] - 1
        score = ret_20d * 2
        return FactorResult(score=score)


class IndustryMomentumFactor(BaseFactor):
    name = "industry_momentum"
    description = "行业动量(基于个股)"
    category = "momentum"
    window_size = 25

    def compute(self, ctx: FactorContext) -> FactorResult:
        closes = extract(ctx.klines, "close")
        if len(closes) < 21:
            return FactorResult(score=0.0)
        score = closes[-1] / closes[-21] - 1
        return FactorResult(score=score)


class MomentumTrendFactor(BaseFactor):
    name = "momentum_trend"
    description = "动量趋势(MA5/MA20)"
    category = "momentum"
    window_size = 25

    def compute(self, ctx: FactorContext) -> FactorResult:
        closes = extract(ctx.klines, "close")
        if len(closes) < 20:
            return FactorResult(score=0.0)
        ma5 = sum(closes[-5:]) / 5
        ma20 = sum(closes[-20:]) / 20
        score = ma5 / ma20 - 1
        return FactorResult(score=score)


class MomentumStrengthFactor(BaseFactor):
    name = "momentum_strength"
    description = "动量强度(上涨天数占比)"
    category = "momentum"
    window_size = 25

    def compute(self, ctx: FactorContext) -> FactorResult:
        closes = extract(ctx.klines, "close")
        if len(closes) < 21:
            return FactorResult(score=0.0)
        up_days = sum(
            1 for i in range(-20, 0) if closes[i] > closes[i - 1]
        )
        score = up_days / 20
        return FactorResult(score=score)


class MomentumConsistencyFactor(BaseFactor):
    name = "momentum_consistency"
    description = "动量一致性(连续上涨天数)"
    category = "momentum"
    window_size = 25

    def compute(self, ctx: FactorContext) -> FactorResult:
        closes = extract(ctx.klines, "close")
        if len(closes) < 21:
            return FactorResult(score=0.0)
        consecutive_up = 0
        for i in range(-1, -21, -1):
            if closes[i] > closes[i - 1]:
                consecutive_up += 1
            else:
                break
        score = consecutive_up / 20
        return FactorResult(score=score)