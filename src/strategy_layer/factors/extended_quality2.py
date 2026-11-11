"""扩展质量因子2: 资产周转率 / 净利润率 / 营收增长 / OCF/净利 / 杠杆 / 速动比率 / EPS / BPS"""
from strategy_layer.factors.base import BaseFactor, FactorContext, FactorResult


class AssetTurnoverFactor(BaseFactor):
    name = "asset_turnover"
    description = "资产周转率"
    category = "quality"
    window_size = 1

    def compute(self, ctx: FactorContext) -> FactorResult:
        if not ctx.financials:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        f = ctx.financials[-1]
        revenue = f["revenue"] if isinstance(f, dict) else f.revenue
        total_assets = f["total_assets"] if isinstance(f, dict) else f.total_assets
        if revenue is None or total_assets is None or total_assets == 0:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        score = revenue / total_assets
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=score, raw_value=score,
            details={"revenue": revenue, "total_assets": total_assets},
        )


class NetProfitMarginFactor(BaseFactor):
    name = "net_profit_margin"
    description = "净利润率"
    category = "quality"
    window_size = 1

    def compute(self, ctx: FactorContext) -> FactorResult:
        if not ctx.financials:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        f = ctx.financials[-1]
        nm = f["net_margin"] if isinstance(f, dict) else f.net_margin
        if nm is None:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=nm, raw_value=nm, details={"net_margin": nm},
        )


class RevenueGrowthStabilityFactor(BaseFactor):
    name = "revenue_growth_stability"
    description = "营收增长稳定性"
    category = "quality"
    window_size = 1

    def compute(self, ctx: FactorContext) -> FactorResult:
        if not ctx.financials:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        f = ctx.financials[-1]
        rg = f["revenue_growth"] if isinstance(f, dict) else f.revenue_growth
        if rg is None:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=rg, raw_value=rg, details={"revenue_growth": rg},
        )


class OperatingCashFlowRatioFactor(BaseFactor):
    name = "ocf_ratio"
    description = "经营现金流/净利润"
    category = "quality"
    window_size = 1

    def compute(self, ctx: FactorContext) -> FactorResult:
        if not ctx.financials:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        f = ctx.financials[-1]
        ocf = f["operating_cash_flow"] if isinstance(f, dict) else f.operating_cash_flow
        profit = f["profit"] if isinstance(f, dict) else f.profit
        if ocf is None or profit is None:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        score = ocf / max(profit, 1)
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=score, raw_value=score,
            details={"operating_cash_flow": ocf, "profit": profit},
        )


class LeverageFactor(BaseFactor):
    name = "financial_leverage"
    description = "财务杠杆"
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


class QuickRatioHealthFactor(BaseFactor):
    name = "quick_ratio_health"
    description = "速动比率健康度"
    category = "quality"
    window_size = 1

    def compute(self, ctx: FactorContext) -> FactorResult:
        if not ctx.financials:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        f = ctx.financials[-1]
        qr = f["quick_ratio"] if isinstance(f, dict) else f.quick_ratio
        if qr is None:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        score = -abs(qr - 1.0)
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=score, raw_value=qr, details={"quick_ratio": qr},
        )


class EarningsPerShareFactor(BaseFactor):
    name = "eps_factor"
    description = "每股收益"
    category = "quality"
    window_size = 1

    def compute(self, ctx: FactorContext) -> FactorResult:
        if not ctx.financials:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        f = ctx.financials[-1]
        eps = f["eps"] if isinstance(f, dict) else f.eps
        if eps is None:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=eps, raw_value=eps, details={"eps": eps},
        )


class BookValuePerShareFactor(BaseFactor):
    name = "bps_factor"
    description = "每股净资产"
    category = "quality"
    window_size = 1

    def compute(self, ctx: FactorContext) -> FactorResult:
        if not ctx.financials:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        f = ctx.financials[-1]
        bps = f["bps"] if isinstance(f, dict) else f.bps
        if bps is None:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=bps, raw_value=bps, details={"bps": bps},
        )