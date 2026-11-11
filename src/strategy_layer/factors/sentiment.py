"""情绪因子: 换手率变化 + 成交量统计"""
from strategy_layer.factors._utils import extract, rolling_mean, rolling_std
from strategy_layer.factors.base import BaseFactor, FactorContext, FactorResult


class SentimentFactor(BaseFactor):
    name = "turnover_ratio"
    description = "短期/长期换手率比值"
    category = "sentiment"
    window_size = 65

    def compute(self, ctx: FactorContext) -> FactorResult:
        if len(ctx.klines) < 20:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        turnovers = [k["turnover_rate"] if isinstance(k, dict) else k.turnover_rate for k in ctx.klines]
        avg_20 = sum(turnovers[-20:]) / 20
        avg_60 = sum(turnovers[-60:]) / 60 if len(turnovers) >= 60 else avg_20
        score = avg_20 / avg_60 if avg_60 > 0 else 1.0
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=score, raw_value=score,
            details={"turnover_20d": avg_20, "turnover_60d": avg_60},
        )


class _VmaBase(BaseFactor):
    window = 5

    def compute(self, ctx: FactorContext) -> FactorResult:
        n = len(ctx.klines)
        w = self.window
        if n < w:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        volumes = extract(ctx.klines, "volume")
        score = float(rolling_mean(volumes, w)[-1])
        return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=score, raw_value=score)


class Vma5Factor(_VmaBase):
    name = "vma5"
    description = "5 日平均成交量"
    category = "sentiment"
    window_size = 5
    window = 5


class Vma10Factor(_VmaBase):
    name = "vma10"
    description = "10 日平均成交量"
    category = "sentiment"
    window_size = 10
    window = 10


class Vma20Factor(_VmaBase):
    name = "vma20"
    description = "20 日平均成交量"
    category = "sentiment"
    window_size = 20
    window = 20


class _VstdBase(BaseFactor):
    window = 5

    def compute(self, ctx: FactorContext) -> FactorResult:
        n = len(ctx.klines)
        w = self.window
        if n < w:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        volumes = extract(ctx.klines, "volume")
        score = float(rolling_std(volumes, w)[-1])
        return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=score, raw_value=score)


class Vstd5Factor(_VstdBase):
    name = "vstd5"
    description = "5 日成交量标准差"
    category = "sentiment"
    window_size = 5
    window = 5


class Vstd10Factor(_VstdBase):
    name = "vstd10"
    description = "10 日成交量标准差"
    category = "sentiment"
    window_size = 10
    window = 10
