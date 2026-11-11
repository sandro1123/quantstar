from strategy_layer.factors._utils import extract
from strategy_layer.factors.base import BaseFactor, FactorContext, FactorResult

EPS = 1e-8


class KmidFactor(BaseFactor):
    name = "kmid"
    description = "(close - open) / |open|"
    category = "technical"
    window_size = 1

    def compute(self, ctx: FactorContext) -> FactorResult:
        if not ctx.klines:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        c = extract(ctx.klines, "close")[-1]
        o = extract(ctx.klines, "open")[-1]
        denom = max(EPS, abs(o))
        score = (c - o) / denom
        return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=score, raw_value=score)


class KlenFactor(BaseFactor):
    name = "klen"
    description = "(high - low) / |open|"
    category = "technical"
    window_size = 1

    def compute(self, ctx: FactorContext) -> FactorResult:
        if not ctx.klines:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        h = extract(ctx.klines, "high")[-1]
        lo = extract(ctx.klines, "low")[-1]
        o = extract(ctx.klines, "open")[-1]
        denom = max(EPS, abs(o))
        score = (h - lo) / denom
        return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=score, raw_value=score)


class Kmid2Factor(BaseFactor):
    name = "kmid2"
    description = "(close - open) / (high - low)"
    category = "technical"
    window_size = 1

    def compute(self, ctx: FactorContext) -> FactorResult:
        if not ctx.klines:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        c = extract(ctx.klines, "close")[-1]
        o = extract(ctx.klines, "open")[-1]
        h = extract(ctx.klines, "high")[-1]
        lo = extract(ctx.klines, "low")[-1]
        denom = max(EPS, h - lo)
        score = (c - o) / denom
        return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=score, raw_value=score)


class KupFactor(BaseFactor):
    name = "kup"
    description = "(high - max(open, close)) / |open|"
    category = "technical"
    window_size = 1

    def compute(self, ctx: FactorContext) -> FactorResult:
        if not ctx.klines:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        h = extract(ctx.klines, "high")[-1]
        o = extract(ctx.klines, "open")[-1]
        c = extract(ctx.klines, "close")[-1]
        denom = max(EPS, abs(o))
        score = (h - max(o, c)) / denom
        return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=score, raw_value=score)


class Kup2Factor(BaseFactor):
    name = "kup2"
    description = "(high - max(open, close)) / (high - low)"
    category = "technical"
    window_size = 1

    def compute(self, ctx: FactorContext) -> FactorResult:
        if not ctx.klines:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        h = extract(ctx.klines, "high")[-1]
        o = extract(ctx.klines, "open")[-1]
        c = extract(ctx.klines, "close")[-1]
        lo = extract(ctx.klines, "low")[-1]
        denom = max(EPS, h - lo)
        score = (h - max(o, c)) / denom
        return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=score, raw_value=score)


class KlowFactor(BaseFactor):
    name = "klow"
    description = "(min(open, close) - low) / |open|"
    category = "technical"
    window_size = 1

    def compute(self, ctx: FactorContext) -> FactorResult:
        if not ctx.klines:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        o = extract(ctx.klines, "open")[-1]
        c = extract(ctx.klines, "close")[-1]
        lo = extract(ctx.klines, "low")[-1]
        denom = max(EPS, abs(o))
        score = (min(o, c) - lo) / denom
        return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=score, raw_value=score)


class Klow2Factor(BaseFactor):
    name = "klow2"
    description = "(min(open, close) - low) / (high - low)"
    category = "technical"
    window_size = 1

    def compute(self, ctx: FactorContext) -> FactorResult:
        if not ctx.klines:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        o = extract(ctx.klines, "open")[-1]
        c = extract(ctx.klines, "close")[-1]
        h = extract(ctx.klines, "high")[-1]
        lo = extract(ctx.klines, "low")[-1]
        denom = max(EPS, h - lo)
        score = (min(o, c) - lo) / denom
        return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=score, raw_value=score)


class KsftFactor(BaseFactor):
    name = "ksft"
    description = "(2*close - high - low) / |open|"
    category = "technical"
    window_size = 1

    def compute(self, ctx: FactorContext) -> FactorResult:
        if not ctx.klines:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        c = extract(ctx.klines, "close")[-1]
        h = extract(ctx.klines, "high")[-1]
        lo = extract(ctx.klines, "low")[-1]
        o = extract(ctx.klines, "open")[-1]
        denom = max(EPS, abs(o))
        score = (2 * c - h - lo) / denom
        return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=score, raw_value=score)


class Ksft2Factor(BaseFactor):
    name = "ksft2"
    description = "(2*close - high - low) / (high - low)"
    category = "technical"
    window_size = 1

    def compute(self, ctx: FactorContext) -> FactorResult:
        if not ctx.klines:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        c = extract(ctx.klines, "close")[-1]
        h = extract(ctx.klines, "high")[-1]
        lo = extract(ctx.klines, "low")[-1]
        denom = max(EPS, h - lo)
        score = (2 * c - h - lo) / denom
        return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=score, raw_value=score)