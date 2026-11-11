"""成长因子: 营收增长 + 利润增长 (TTM同比)"""
from dataclasses import asdict, is_dataclass

from strategy_layer.factors.base import BaseFactor, FactorContext, FactorResult
from strategy_layer.factors.financial_caliber import get_latest_financial_date, get_ttm_value


class GrowthFactor(BaseFactor):
    name = "growth_combined"
    description = "营收TTM增长 + 利润TTM增长"
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

            cur_rev = get_ttm_value(f_list, "revenue", year, quarter)
            cur_prof = get_ttm_value(f_list, "profit", year, quarter)
            prev_rev = get_ttm_value(f_list, "revenue", year - 1, quarter)
            prev_prof = get_ttm_value(f_list, "profit", year - 1, quarter)

            revenue_growth = (cur_rev - prev_rev) / prev_rev if prev_rev else 0
            profit_growth = (cur_prof - prev_prof) / prev_prof if prev_prof else 0

            score = revenue_growth + profit_growth
            return FactorResult(
                code=ctx.code, date=ctx.date, factor_name=self.name,
                score=score, raw_value=score,
                details={
                    "revenue_growth_ttm": revenue_growth, "profit_growth_ttm": profit_growth,
                    "cur_revenue_ttm": cur_rev, "prev_revenue_ttm": prev_rev,
                    "cur_profit_ttm": cur_prof, "prev_profit_ttm": prev_prof,
                },
            )
        except Exception:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)