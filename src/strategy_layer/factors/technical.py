from strategy_layer.factors._utils import ema, extract, rolling_mean, rolling_std
from strategy_layer.factors.base import BaseFactor, FactorContext, FactorResult

EPS = 1e-8


class Rsi14Factor(BaseFactor):
    name = "rsi14"
    description = "14 日 RSI"
    category = "technical"
    window_size = 14

    def compute(self, ctx: FactorContext) -> FactorResult:
        n = len(ctx.klines)
        if n < 15:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        closes = extract(ctx.klines, "close")
        returns = [0.0] * n
        for i in range(1, n):
            denom = max(EPS, abs(closes[i - 1]))
            returns[i] = (closes[i] - closes[i - 1]) / denom
        gain = [max(0, r) for r in returns]
        loss = [-r if r < 0 else 0 for r in returns]
        ag = rolling_mean(gain, 14)
        al = rolling_mean(loss, 14)
        score = 100 - 100 / (1 + ag[-1] / al[-1]) if al[-1] > EPS else 100.0
        return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=score, raw_value=score)


class MacdHistFactor(BaseFactor):
    name = "macd_hist"
    description = "MACD 柱状图 (DIF - DEA)"
    category = "technical"
    window_size = 26

    def compute(self, ctx: FactorContext) -> FactorResult:
        n = len(ctx.klines)
        if n < 26:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        closes = extract(ctx.klines, "close")
        e12 = ema(closes, 12)
        e26 = ema(closes, 26)
        macd_line = [e12[i] - e26[i] for i in range(n)]
        signal = ema(macd_line, 9)
        score = macd_line[-1] - signal[-1]
        return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=score, raw_value=score)


class BbWidthFactor(BaseFactor):
    name = "bb_width"
    description = "布林带宽度 / |中轨|"
    category = "technical"
    window_size = 20

    def compute(self, ctx: FactorContext) -> FactorResult:
        n = len(ctx.klines)
        if n < 20:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        closes = extract(ctx.klines, "close")
        bbm = rolling_mean(closes, 20)[-1]
        bbs = rolling_std(closes, 20)[-1]
        denom = max(EPS, abs(bbm))
        score = ((bbm + 2 * bbs) - (bbm - 2 * bbs)) / denom
        return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=score, raw_value=score)