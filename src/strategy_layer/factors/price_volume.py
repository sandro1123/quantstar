from strategy_layer.factors._utils import extract, rolling_std
from strategy_layer.factors.base import BaseFactor, FactorContext, FactorResult

EPS = 1e-8


def _pearson_num(x, y, w):
    n = len(x)
    x_slice = x[n-w:n]
    y_slice = y[n-w:n]
    x_mean = sum(x_slice) / w
    y_mean = sum(y_slice) / w
    return sum((x_slice[i] - x_mean) * (y_slice[i] - y_mean) for i in range(w))


def _build_returns(closes):
    n = len(closes)
    returns = [0.0] * n
    for i in range(1, n):
        denom = max(EPS, abs(closes[i - 1]))
        returns[i] = (closes[i] - closes[i - 1]) / denom
    return returns


def _build_vol_returns(volumes):
    n = len(volumes)
    vr = [0.0] * n
    for i in range(1, n):
        denom = max(EPS, volumes[i - 1])
        vr[i] = (volumes[i] - volumes[i - 1]) / denom
    return vr


class Corr5Factor(BaseFactor):
    name = "corr5"
    description = "5 日收益率与量收益率相关系数"
    category = "technical"
    window_size = 5

    def compute(self, ctx: FactorContext) -> FactorResult:
        n = len(ctx.klines)
        if n < 5:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        closes = extract(ctx.klines, "close")
        volumes = extract(ctx.klines, "volume")
        rets = _build_returns(closes)
        vr = _build_vol_returns(volumes)
        w = 5
        corr_num = _pearson_num(rets, vr, w)
        std_r = rolling_std(rets, w)[-1]
        std_vr = rolling_std(vr, w)[-1]
        denom = w * std_r * std_vr
        score = corr_num / denom if denom > EPS else 0.0
        return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=score, raw_value=score)


class Corr10Factor(BaseFactor):
    name = "corr10"
    description = "10 日收益率与量收益率相关系数"
    category = "technical"
    window_size = 10

    def compute(self, ctx: FactorContext) -> FactorResult:
        n = len(ctx.klines)
        if n < 10:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        closes = extract(ctx.klines, "close")
        volumes = extract(ctx.klines, "volume")
        rets = _build_returns(closes)
        vr = _build_vol_returns(volumes)
        w = 10
        corr_num = _pearson_num(rets, vr, w)
        std_r = rolling_std(rets, w)[-1]
        std_vr = rolling_std(vr, w)[-1]
        denom = w * std_r * std_vr
        score = corr_num / denom if denom > EPS else 0.0
        return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=score, raw_value=score)


class Cord5Factor(BaseFactor):
    name = "cord5"
    description = "5 日绝对收益率与绝对量收益率相关系数"
    category = "technical"
    window_size = 5

    def compute(self, ctx: FactorContext) -> FactorResult:
        n = len(ctx.klines)
        if n < 5:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        closes = extract(ctx.klines, "close")
        volumes = extract(ctx.klines, "volume")
        rets = _build_returns(closes)
        vr = _build_vol_returns(volumes)
        w = 5
        abs_rets = [abs(r) for r in rets]
        abs_vr = [abs(v) for v in vr]
        cord_num = _pearson_num(abs_rets, abs_vr, w)
        std_abs_r = rolling_std(abs_rets, w)[-1]
        std_abs_vr = rolling_std(abs_vr, w)[-1]
        denom = w * std_abs_r * std_abs_vr
        score = cord_num / denom if denom > EPS else 0.0
        return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=score, raw_value=score)


class Cord20Factor(BaseFactor):
    name = "cord20"
    description = "20 日绝对收益率与绝对量收益率相关系数"
    category = "technical"
    window_size = 20

    def compute(self, ctx: FactorContext) -> FactorResult:
        n = len(ctx.klines)
        if n < 20:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        closes = extract(ctx.klines, "close")
        volumes = extract(ctx.klines, "volume")
        rets = _build_returns(closes)
        vr = _build_vol_returns(volumes)
        w = 20
        abs_rets = [abs(r) for r in rets]
        abs_vr = [abs(v) for v in vr]
        cord_num = _pearson_num(abs_rets, abs_vr, w)
        std_abs_r = rolling_std(abs_rets, w)[-1]
        std_abs_vr = rolling_std(abs_vr, w)[-1]
        denom = w * std_abs_r * std_abs_vr
        score = cord_num / denom if denom > EPS else 0.0
        return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=score, raw_value=score)