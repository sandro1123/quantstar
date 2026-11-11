"""波动率因子: 20 日波动率 + 5 日反转 + 滚动标准差"""
import math

from strategy_layer.factors._utils import extract, rolling_std
from strategy_layer.factors.base import BaseFactor, FactorContext, FactorResult


class VolatilityFactor(BaseFactor):
    name = "volatility_reversal"
    description = "低波动 + 短期反转"
    category = "technical"
    window_size = 25

    def compute(self, ctx: FactorContext) -> FactorResult:
        if len(ctx.klines) < 20:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        closes = [k["close"] if isinstance(k, dict) else k.close for k in ctx.klines]
        returns = [(closes[i] / closes[i - 1] - 1) for i in range(max(1, len(closes) - 20), len(closes))]
        mean_r = sum(returns) / len(returns) if returns else 0
        vol_20d = math.sqrt(sum((r - mean_r) ** 2 for r in returns) / len(returns)) if returns else 0
        reversal_5d = -(closes[-1] / closes[-5] - 1) if len(closes) >= 5 else 0
        score = -vol_20d * 0.6 + reversal_5d * 0.4
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=score, raw_value=vol_20d,
            details={"vol_20d": vol_20d, "reversal_5d": reversal_5d},
        )


EPS = 1e-8


class _StdBase(BaseFactor):
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
        score = float(rolling_std(returns, w)[-1])
        return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=score, raw_value=score)


class Std5Factor(_StdBase):
    name = "std5"
    description = "5 日收益率标准差"
    category = "technical"
    window_size = 5
    window = 5


class Std10Factor(_StdBase):
    name = "std10"
    description = "10 日收益率标准差"
    category = "technical"
    window_size = 10
    window = 10


class Std20Factor(_StdBase):
    name = "std20"
    description = "20 日收益率标准差"
    category = "technical"
    window_size = 20
    window = 20
