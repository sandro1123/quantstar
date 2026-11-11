"""扩展价值因子: EP/BP/SP/CFP/DividendYield"""
from strategy_layer.factors.base import BaseFactor, FactorContext, FactorResult, is_valid_positive


class EPFactor(BaseFactor):
    """Earnings-to-Price (EPS/Price) — 越高越便宜"""
    name = "ep_ratio"
    description = "盈利收益率 (EPS/Price)，越高越便宜"
    category = "value"
    window_size = 1

    def compute(self, ctx: FactorContext) -> FactorResult:
        if not ctx.valuations:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=None, raw_value=None)
        v = ctx.valuations[-1]
        pe = v.get("pe_ttm") if isinstance(v, dict) else getattr(v, "pe_ttm", None)
        if not is_valid_positive(pe):
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=None, raw_value=None, details={"pe_ttm": pe})
        score = -pe
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=score, raw_value=pe,
            details={"pe_ttm": pe},
        )


class BPFactor(BaseFactor):
    """Book-to-Price (BPS/Price) — 越高越便宜"""
    name = "bp_ratio"
    description = "净资产收益率 (BPS/Price)，越高越便宜"
    category = "value"
    window_size = 1

    def compute(self, ctx: FactorContext) -> FactorResult:
        if not ctx.valuations:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=None, raw_value=None)
        v = ctx.valuations[-1]
        pb = v.get("pb_mrq") if isinstance(v, dict) else getattr(v, "pb_mrq", None)
        if not is_valid_positive(pb):
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=None, raw_value=None, details={"pb_mrq": pb})
        score = -pb
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=score, raw_value=pb,
            details={"pb_mrq": pb},
        )


class SPFactor(BaseFactor):
    """Sales-to-Price (RevenuePerShare/Price) — 越高越便宜"""
    name = "sp_ratio"
    description = "营收收益率 (RevenuePerShare/Price)，越高越便宜"
    category = "value"
    window_size = 1

    def compute(self, ctx: FactorContext) -> FactorResult:
        if not ctx.valuations:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=None, raw_value=None)
        v = ctx.valuations[-1]
        ps = v.get("ps_ttm") if isinstance(v, dict) else getattr(v, "ps_ttm", None)
        if not is_valid_positive(ps):
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=None, raw_value=None, details={"ps_ttm": ps})
        score = -ps
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=score, raw_value=ps,
            details={"ps_ttm": ps},
        )


class CFPFactor(BaseFactor):
    """Cash-Flow-to-Price — 越高越便宜"""
    name = "cfp_ratio"
    description = "现金流收益率 (CashFlow/Price)，越高越便宜"
    category = "value"
    window_size = 1

    def compute(self, ctx: FactorContext) -> FactorResult:
        if not ctx.valuations:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=None, raw_value=None)
        v = ctx.valuations[-1]
        pcf = v.get("pcf_ttm") if isinstance(v, dict) else getattr(v, "pcf_ttm", None)
        if not is_valid_positive(pcf):
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=None, raw_value=None, details={"pcf_ttm": pcf})
        score = -pcf
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=score, raw_value=pcf,
            details={"pcf_ttm": pcf},
        )


class DividendYieldFactor(BaseFactor):
    """Dividend Yield — 越高越好"""
    name = "dividend_yield"
    description = "股息率，越高越好"
    category = "value"
    window_size = 1

    def compute(self, ctx: FactorContext) -> FactorResult:
        if not ctx.valuations:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=None, raw_value=None)
        v = ctx.valuations[-1]
        dy = v.get("dividend_yield") if isinstance(v, dict) else getattr(v, "dividend_yield", None)
        if not is_valid_positive(dy):
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=None, raw_value=None, details={"dividend_yield": dy})
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=dy, raw_value=dy,
            details={"dividend_yield": dy},
        )
