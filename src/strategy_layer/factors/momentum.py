"""动量因子: 20 日累计收益率 + 滚动收益率"""
from strategy_layer.factors._utils import extract, rolling_sum
from strategy_layer.factors.base import BaseFactor, FactorContext, FactorResult


class MomentumFactor(BaseFactor):
    name = "momentum_20d"
    description = "20 个交易日累计收益率"
    category = "momentum"
    window_size = 25

    def compute(self, ctx: FactorContext) -> FactorResult:
        if len(ctx.klines) < 20:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        closes = [k["close"] if isinstance(k, dict) else k.close for k in ctx.klines]
        if closes[-20] == 0:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        score = closes[-1] / closes[-20] - 1
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=score, raw_value=score,
            details={"close_today": closes[-1], "close_20d_ago": closes[-20]},
        )


EPS = 1e-8


class _RocBase(BaseFactor):
    window = 5

    def compute(self, ctx: FactorContext) -> FactorResult:
        n = len(ctx.klines)
        w = self.window
        if n < w:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        closes = extract(ctx.klines, "close")
        returns = [0.0] * n
        for i in range(1, n):
            denom = max(EPS, abs(closes[i - 1]))
            returns[i] = (closes[i] - closes[i - 1]) / denom
        score = float(rolling_sum(returns, w)[-1])
        return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=score, raw_value=score)


class Roc5Factor(_RocBase):
    name = "roc5"
    description = "5 日累计收益率"
    category = "momentum"
    window_size = 5
    window = 5


class Roc10Factor(_RocBase):
    name = "roc10"
    description = "10 日累计收益率"
    category = "momentum"
    window_size = 10
    window = 10


class Roc20Factor(_RocBase):
    name = "roc20"
    description = "20 日累计收益率"
    category = "momentum"
    window_size = 20
    window = 20


class Roc60Factor(_RocBase):
    name = "roc60"
    description = "60 日累计收益率"
    category = "momentum"
    window_size = 60
    window = 60
