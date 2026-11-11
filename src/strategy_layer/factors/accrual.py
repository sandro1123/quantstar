"""应计因子: 经营现金流 / 净利润 (TTM口径)"""
from dataclasses import asdict, is_dataclass

from strategy_layer.factors.base import BaseFactor, FactorContext, FactorResult
from strategy_layer.factors.financial_caliber import get_latest_financial_date, get_ttm_value


class AccrualFactor(BaseFactor):
    name = "accrual_quality"
    description = "应计质量（经营现金流 / 净利润，TTM口径）"
    category = "quality"
    window_size = 1

    def compute(self, ctx: FactorContext) -> FactorResult:
        try:
            if not ctx.financials:
                return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
            # DL-61: Financial 是 dataclass, dict(f) 抛 TypeError 被 except 吞掉恒 0。
            # 用 asdict 正确转换 (dict 直通)。
            f_list = [asdict(f) if is_dataclass(f) and not isinstance(f, type) else f for f in ctx.financials]
            year, quarter = get_latest_financial_date(f_list)
            if year == 0:
                return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
            op_cf = get_ttm_value(f_list, "operating_cash_flow", year, quarter)
            profit = get_ttm_value(f_list, "profit", year, quarter)
            if not profit or profit == 0:
                return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
            ratio = op_cf / profit if profit else 0
            score = min(ratio, 5.0)
            return FactorResult(
                code=ctx.code, date=ctx.date, factor_name=self.name,
                score=score, raw_value=ratio,
                details={"op_cf_ttm": op_cf, "profit_ttm": profit, "ratio": ratio},
            )
        except Exception:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)