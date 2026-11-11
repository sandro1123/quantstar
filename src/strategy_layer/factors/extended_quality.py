"""扩展质量因子: ROA / 营业利润率 / 盈利稳定性 / 低负债率 / 流动比率健康度"""
import statistics

from strategy_layer.factors.base import BaseFactor, FactorContext, FactorResult


class ROAFactor(BaseFactor):
    name = "roa"
    description = "资产回报率(ROA)"
    category = "quality"
    window_size = 1

    def compute(self, ctx: FactorContext) -> FactorResult:
        if not ctx.financials:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        f = ctx.financials[-1]
        roa = f["roa"] if isinstance(f, dict) else f.roa
        if roa is None:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=roa, raw_value=roa, details={"roa": roa},
        )


class OperatingMarginFactor(BaseFactor):
    name = "operating_margin"
    description = "营业利润率"
    category = "quality"
    window_size = 1

    def compute(self, ctx: FactorContext) -> FactorResult:
        if not ctx.financials:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        f = ctx.financials[-1]
        om = f["operating_margin"] if isinstance(f, dict) else f.operating_margin
        if om is None:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=om, raw_value=om, details={"operating_margin": om},
        )


class EarningsStabilityFactor(BaseFactor):
    name = "earnings_stability"
    description = "盈利稳定性(近4季EPS标准差)"
    category = "quality"
    window_size = 1

    def compute(self, ctx: FactorContext) -> FactorResult:
        if not ctx.financials or len(ctx.financials) < 2:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        eps_values = []
        for f in ctx.financials[-4:]:
            eps = f["eps"] if isinstance(f, dict) else f.eps
            if eps is not None:
                eps_values.append(eps)
        if len(eps_values) < 2:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        std = statistics.stdev(eps_values)
        score = -std
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=score, raw_value=std,
            details={"eps_stdev": std, "eps_count": len(eps_values)},
        )


class DebtRatioFactor(BaseFactor):
    name = "low_debt_ratio"
    description = "低负债率"
    category = "quality"
    window_size = 1

    def compute(self, ctx: FactorContext) -> FactorResult:
        if not ctx.financials:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        f = ctx.financials[-1]
        dr = f["debt_ratio"] if isinstance(f, dict) else f.debt_ratio
        if dr is None:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        score = -dr
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=score, raw_value=dr, details={"debt_ratio": dr},
        )


class CurrentRatioFactor(BaseFactor):
    name = "current_ratio_factor"
    description = "流动比率健康度"
    category = "quality"
    window_size = 1

    def compute(self, ctx: FactorContext) -> FactorResult:
        if not ctx.financials:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        f = ctx.financials[-1]
        cr = f["current_ratio"] if isinstance(f, dict) else f.current_ratio
        if cr is None:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        score = -abs(cr - 2.0)
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=score, raw_value=cr, details={"current_ratio": cr},
        )
