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


class _SumpBase(BaseFactor):
    kind = "p"
    window = 5

    def compute(self, ctx: FactorContext) -> FactorResult:
        n = len(ctx.klines)
        w = self.window
        if n < w:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        closes = extract(ctx.klines, "close")
        rets = _build_returns(closes)[n - w + 1:n + 1]
        sum_up = sum(r for r in rets if r > 0)
        sum_dn = sum(abs(r) for r in rets if r < 0)
        total = sum_up + sum_dn
        if self.kind == "p":
            score = sum_up / total if total > EPS else 0.0
        else:
            score = (sum_up - sum_dn) / total if total > EPS else 0.0
        return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=score, raw_value=score)


class Sump5Factor(_SumpBase):
    name = "sump5"
    description = "5 日上涨幅度占比"
    category = "technical"
    window_size = 5
    kind = "p"
    window = 5


class Sump10Factor(_SumpBase):
    name = "sump10"
    description = "10 日上涨幅度占比"
    category = "technical"
    window_size = 10
    kind = "p"
    window = 10


class Sump20Factor(_SumpBase):
    name = "sump20"
    description = "20 日上涨幅度占比"
    category = "technical"
    window_size = 20
    kind = "p"
    window = 20


class Sumd5Factor(_SumpBase):
    name = "sumd5"
    description = "5 日涨跌幅差/总幅度"
    category = "technical"
    window_size = 5
    kind = "d"
    window = 5


class Sumd10Factor(_SumpBase):
    name = "sumd10"
    description = "10 日涨跌幅差/总幅度"
    category = "technical"
    window_size = 10
    kind = "d"
    window = 10


class Sumd20Factor(_SumpBase):
    name = "sumd20"
    description = "20 日涨跌幅差/总幅度"
    category = "technical"
    window_size = 20
    kind = "d"
    window = 20