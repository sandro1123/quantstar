"""扩展价值因子 II: FCF/EV, EV/EBITDA, PEG, 收益率类, 市值类, 总回报"""
import math

from strategy_layer.factors.base import (
    BaseFactor,
    FactorContext,
    FactorResult,
    is_finite_number,
    is_valid_nonzero,
    is_valid_positive,
)


class FcfEvFactor(BaseFactor):
    """Free Cash Flow / Enterprise Value — 越高越便宜"""
    name = "fcf_ev"
    description = "自由现金流/企业价值，越高越便宜"
    category = "value"
    window_size = 1

    def compute(self, ctx: FactorContext) -> FactorResult:
        if not ctx.valuations or not ctx.financials:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=None, raw_value=None)
        v = ctx.valuations[-1]
        f = ctx.financials[-1]
        mc = v.get("market_cap") if isinstance(v, dict) else getattr(v, "market_cap", None)
        tl = f.get("total_liabilities") if isinstance(f, dict) else getattr(f, "total_liabilities", None)
        cfps = f.get("cash_flow_per_share") if isinstance(f, dict) else getattr(f, "cash_flow_per_share", None)
        if not is_valid_positive(mc) or not is_valid_nonzero(tl) or not is_valid_nonzero(cfps):
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=None, raw_value=None,
                                details={"market_cap": mc, "total_liabilities": tl, "cash_flow_per_share": cfps})
        ev = mc + tl
        if ev <= 0:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=None, raw_value=None,
                                details={"market_cap": mc, "total_liabilities": tl, "cash_flow_per_share": cfps, "ev": ev})
        score = cfps / ev
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=score, raw_value=score,
            details={"market_cap": mc, "total_liabilities": tl, "cash_flow_per_share": cfps, "ev": ev},
        )


class EvEbitdaFactor(BaseFactor):
    """EV/EBITDA — 越低越便宜"""
    name = "ev_ebitda"
    description = "企业价值/EBITDA，越低越便宜"
    category = "value"
    window_size = 1

    def compute(self, ctx: FactorContext) -> FactorResult:
        if not ctx.valuations or not ctx.financials:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=None, raw_value=None)
        v = ctx.valuations[-1]
        f = ctx.financials[-1]
        mc = v.get("market_cap") if isinstance(v, dict) else getattr(v, "market_cap", None)
        tl = f.get("total_liabilities") if isinstance(f, dict) else getattr(f, "total_liabilities", None)
        profit = f.get("profit") if isinstance(f, dict) else getattr(f, "profit", None)
        debt_ratio = f.get("debt_ratio") if isinstance(f, dict) else getattr(f, "debt_ratio", None)
        ta = f.get("total_assets") if isinstance(f, dict) else getattr(f, "total_assets", None)
        if not is_valid_positive(mc) or not is_valid_nonzero(tl) or not is_valid_nonzero(profit):
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=None, raw_value=None,
                                details={"market_cap": mc, "total_liabilities": tl, "profit": profit})
        ev = mc + tl
        interest_approx = (debt_ratio or 0) * (ta or 0) * 0.05
        ebitda = profit + interest_approx
        if not is_finite_number(ebitda) or ebitda <= 0:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=None, raw_value=None,
                                details={"ev": ev, "ebitda": ebitda})
        ev_ebitda = ev / ebitda
        score = -ev_ebitda
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=score, raw_value=ev_ebitda,
            details={"ev": ev, "ebitda": ebitda, "ev_ebitda": ev_ebitda},
        )


class PegFactor(BaseFactor):
    """PEG ratio (PE / Growth) — 越低越便宜"""
    name = "peg_ratio"
    description = "PEG比率 (PE/营收增长率)，越低越便宜"
    category = "value"
    window_size = 1

    def compute(self, ctx: FactorContext) -> FactorResult:
        if not ctx.valuations or not ctx.financials:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=None, raw_value=None)
        v = ctx.valuations[-1]
        f = ctx.financials[-1]
        pe = v.get("pe_ttm") if isinstance(v, dict) else getattr(v, "pe_ttm", None)
        growth = f.get("revenue_growth") if isinstance(f, dict) else getattr(f, "revenue_growth", None)
        if not is_valid_positive(pe) or not is_valid_positive(growth):
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=None, raw_value=None,
                                details={"pe_ttm": pe, "revenue_growth": growth})
        peg = pe / growth
        score = -peg
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=score, raw_value=peg,
            details={"pe_ttm": pe, "revenue_growth": growth, "peg": peg},
        )


class EarningsYieldFactor(BaseFactor):
    """Earnings Yield (E/P) — 越高越好"""
    name = "earnings_yield"
    description = "盈利收益率 (1/PE)，越高越便宜"
    category = "value"
    window_size = 1

    def compute(self, ctx: FactorContext) -> FactorResult:
        if not ctx.valuations:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=None, raw_value=None)
        v = ctx.valuations[-1]
        pe = v.get("pe_ttm") if isinstance(v, dict) else getattr(v, "pe_ttm", None)
        if not is_valid_positive(pe):
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=None, raw_value=None,
                                details={"pe_ttm": pe})
        score = 1.0 / pe
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=score, raw_value=score,
            details={"pe_ttm": pe},
        )


class BookYieldFactor(BaseFactor):
    """Book Yield (B/P) — 越高越好"""
    name = "book_yield"
    description = "账面价值收益率 (1/PB)，越高越便宜"
    category = "value"
    window_size = 1

    def compute(self, ctx: FactorContext) -> FactorResult:
        if not ctx.valuations:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=None, raw_value=None)
        v = ctx.valuations[-1]
        pb = v.get("pb_mrq") if isinstance(v, dict) else getattr(v, "pb_mrq", None)
        if not is_valid_positive(pb):
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=None, raw_value=None,
                                details={"pb_mrq": pb})
        score = 1.0 / pb
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=score, raw_value=score,
            details={"pb_mrq": pb},
        )


class RevenueYieldFactor(BaseFactor):
    """Revenue Yield (S/P) — 越高越好"""
    name = "revenue_yield"
    description = "营收收益率 (1/PS)，越高越便宜"
    category = "value"
    window_size = 1

    def compute(self, ctx: FactorContext) -> FactorResult:
        if not ctx.valuations:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=None, raw_value=None)
        v = ctx.valuations[-1]
        ps = v.get("ps_ttm") if isinstance(v, dict) else getattr(v, "ps_ttm", None)
        if not is_valid_positive(ps):
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=None, raw_value=None,
                                details={"ps_ttm": ps})
        score = 1.0 / ps
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=score, raw_value=score,
            details={"ps_ttm": ps},
        )


class CashFlowYieldFactor(BaseFactor):
    """Cash Flow Yield (CF/P) — 越高越好"""
    name = "cash_flow_yield"
    description = "现金流收益率 (1/PCF)，越高越便宜"
    category = "value"
    window_size = 1

    def compute(self, ctx: FactorContext) -> FactorResult:
        if not ctx.valuations:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=None, raw_value=None)
        v = ctx.valuations[-1]
        pcf = v.get("pcf_ttm") if isinstance(v, dict) else getattr(v, "pcf_ttm", None)
        if not is_valid_positive(pcf):
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=None, raw_value=None,
                                details={"pcf_ttm": pcf})
        score = 1.0 / pcf
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=score, raw_value=score,
            details={"pcf_ttm": pcf},
        )


class MarketCapFactor(BaseFactor):
    """Market cap factor (small cap premium) — 市值越小越好"""
    name = "market_cap_factor"
    description = "市值因子，小市值溢价"
    category = "value"
    window_size = 1

    def compute(self, ctx: FactorContext) -> FactorResult:
        if not ctx.valuations:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=None, raw_value=None)
        v = ctx.valuations[-1]
        mc = v.get("market_cap") if isinstance(v, dict) else getattr(v, "market_cap", None)
        if not is_valid_positive(mc):
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=None, raw_value=None,
                                details={"market_cap": mc})
        score = -math.log(mc)
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=score, raw_value=mc,
            details={"market_cap": mc},
        )


class EnterpriseValueFactor(BaseFactor):
    """Enterprise Value factor — EV 越小越好"""
    name = "enterprise_value"
    description = "企业价值因子，小EV溢价"
    category = "value"
    window_size = 1

    def compute(self, ctx: FactorContext) -> FactorResult:
        if not ctx.valuations or not ctx.financials:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=None, raw_value=None)
        v = ctx.valuations[-1]
        f = ctx.financials[-1]
        mc = v.get("market_cap") if isinstance(v, dict) else getattr(v, "market_cap", None)
        tl = f.get("total_liabilities") if isinstance(f, dict) else getattr(f, "total_liabilities", None)
        if not is_valid_positive(mc) or not is_valid_nonzero(tl):
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=None, raw_value=None,
                                details={"market_cap": mc, "total_liabilities": tl})
        ev = mc + tl
        if ev <= 0:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=None, raw_value=None,
                                details={"market_cap": mc, "total_liabilities": tl, "ev": ev})
        score = -math.log(ev)
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=score, raw_value=ev,
            details={"market_cap": mc, "total_liabilities": tl, "ev": ev},
        )


class TotalReturnFactor(BaseFactor):
    """Total return (dividend yield + earnings yield) — 越高越好"""
    name = "total_return"
    description = "总回报率 (股息率+盈利收益率)，越高越好"
    category = "value"
    window_size = 1

    def compute(self, ctx: FactorContext) -> FactorResult:
        if not ctx.valuations:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=None, raw_value=None)
        v = ctx.valuations[-1]
        if isinstance(v, dict):
            dy = v.get("dividend_yield")
            pe = v.get("pe_ttm")
        else:
            dy = getattr(v, "dividend_yield", None)
            pe = getattr(v, "pe_ttm", None)
        used = []
        missing = []
        dy_part = 0.0
        if is_finite_number(dy):
            dy_part = dy
            used.append("dividend_yield")
        else:
            missing.append("dividend_yield")
        earnings_yield = 0.0
        if is_valid_positive(pe):
            earnings_yield = 1.0 / pe
            used.append("pe_ttm")
        else:
            missing.append("pe_ttm")
        if not used:
            return FactorResult(
                code=ctx.code, date=ctx.date, factor_name=self.name,
                score=None, raw_value=None,
                details={"dividend_yield": dy, "pe_ttm": pe, "used": used, "missing": missing},
            )
        score = dy_part + earnings_yield
        if score <= 0:
            return FactorResult(
                code=ctx.code, date=ctx.date, factor_name=self.name,
                score=None, raw_value=None,
                details={"dividend_yield": dy, "pe_ttm": pe, "earnings_yield": earnings_yield, "used": used, "missing": missing},
            )
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=score, raw_value=score,
            details={"dividend_yield": dy, "pe_ttm": pe, "earnings_yield": earnings_yield, "used": used, "missing": missing},
        )
