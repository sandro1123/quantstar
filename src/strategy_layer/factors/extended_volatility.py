"""扩展波动率因子: 实现波动率 / 下行波动率 / 波动率的波动率 / 最大回撤"""
import math

from strategy_layer.factors._utils import extract
from strategy_layer.factors.base import BaseFactor, FactorContext, FactorResult


def _daily_returns(closes):
    return [(closes[i] / closes[i - 1] - 1) for i in range(1, len(closes))]


def _stdev(values):
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    return math.sqrt(sum((v - mean) ** 2 for v in values) / n)


class RealizedVol20Factor(BaseFactor):
    name = "realized_vol_20d"
    description = "20日实现波动率"
    category = "volatility"
    window_size = 25

    def compute(self, ctx: FactorContext) -> FactorResult:
        closes = extract(ctx.klines, "close")
        if len(closes) < 21:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        returns = _daily_returns(closes)[-20:]
        vol = _stdev(returns)
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=-vol, raw_value=vol,
            details={"vol_20d": vol},
        )


class RealizedVol60Factor(BaseFactor):
    name = "realized_vol_60d"
    description = "60日实现波动率"
    category = "volatility"
    window_size = 65

    def compute(self, ctx: FactorContext) -> FactorResult:
        closes = extract(ctx.klines, "close")
        if len(closes) < 61:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        returns = _daily_returns(closes)[-60:]
        vol = _stdev(returns)
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=-vol, raw_value=vol,
            details={"vol_60d": vol},
        )


class DownsideVolFactor(BaseFactor):
    name = "downside_vol"
    description = "下行波动率(仅负收益)"
    category = "volatility"
    window_size = 25

    def compute(self, ctx: FactorContext) -> FactorResult:
        closes = extract(ctx.klines, "close")
        if len(closes) < 21:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        returns = _daily_returns(closes)[-20:]
        neg = [r for r in returns if r < 0]
        downside_vol = _stdev(neg) if len(neg) >= 2 else 0.0
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=-downside_vol, raw_value=downside_vol,
            details={"downside_vol": downside_vol},
        )


class VolOfVolFactor(BaseFactor):
    name = "vol_of_vol"
    description = "波动率的波动率"
    category = "volatility"
    window_size = 50

    def compute(self, ctx: FactorContext) -> FactorResult:
        closes = extract(ctx.klines, "close")
        if len(closes) < 31:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        returns = _daily_returns(closes)
        window = 10
        vols = []
        for i in range(window, len(returns)):
            vols.append(_stdev(returns[i - window: i]))
        vol_of_vol = _stdev(vols) if len(vols) >= 2 else 0.0
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=-vol_of_vol, raw_value=vol_of_vol,
            details={"vol_of_vol": vol_of_vol},
        )


class MaxDrawdownFactor(BaseFactor):
    name = "max_drawdown_20d"
    description = "20日最大回撤"
    category = "volatility"
    window_size = 25

    def compute(self, ctx: FactorContext) -> FactorResult:
        closes = extract(ctx.klines, "close")
        if len(closes) < 21:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        window = closes[-20:]
        peak = window[0]
        max_dd = 0.0
        for c in window:
            peak = max(peak, c)
            dd = (peak - c) / peak if peak > 0 else 0.0
            max_dd = max(max_dd, dd)
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=-max_dd, raw_value=max_dd,
            details={"max_drawdown": max_dd},
        )
