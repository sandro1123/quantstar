from strategy_layer.factors._utils import extract
from strategy_layer.factors.base import BaseFactor, FactorContext, FactorResult

EPS = 1e-8


def _build_returns(closes):
    n = len(closes)
    returns = [0.0] * n
    for i in range(1, n):
        denom = max(EPS, abs(closes[i - 1]))
        returns[i] = (closes[i] - closes[i - 1]) / denom
    return returns


class _CntBase(BaseFactor):
    kind = "p"
    window = 5

    def compute(self, ctx: FactorContext) -> FactorResult:
        n = len(ctx.klines)
        w = self.window
        if n < w:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        closes = extract(ctx.klines, "close")
        rets = _build_returns(closes)[n - w + 1:n + 1]
        up_cnt = sum(1 for r in rets if r > 0)
        dn_cnt = sum(1 for r in rets if r < 0)
        total = up_cnt + dn_cnt
        if self.kind == "p":
            score = up_cnt / total if total > 0 else 0.0
        else:
            score = (up_cnt - dn_cnt) / total if total > 0 else 0.0
        return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=score, raw_value=score)


class Cntp5Factor(_CntBase):
    name = "cntp5"
    description = "5 日上涨天数占比"
    category = "technical"
    window_size = 5
    kind = "p"
    window = 5


class Cntp10Factor(_CntBase):
    name = "cntp10"
    description = "10 日上涨天数占比"
    category = "technical"
    window_size = 10
    kind = "p"
    window = 10


class Cntp20Factor(_CntBase):
    name = "cntp20"
    description = "20 日上涨天数占比"
    category = "technical"
    window_size = 20
    kind = "p"
    window = 20


class Cntd5Factor(_CntBase):
    name = "cntd5"
    description = "5 日涨跌差/总数"
    category = "technical"
    window_size = 5
    kind = "d"
    window = 5


class Cntd10Factor(_CntBase):
    name = "cntd10"
    description = "10 日涨跌差/总数"
    category = "technical"
    window_size = 10
    kind = "d"
    window = 10


class Cntd20Factor(_CntBase):
    name = "cntd20"
    description = "20 日涨跌差/总数"
    category = "technical"
    window_size = 20
    kind = "d"
    window = 20