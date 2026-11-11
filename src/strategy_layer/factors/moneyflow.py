"""资金流因子 — 基于 stock_moneyflow 表的资金流向"""
from strategy_layer.factors.base import BaseFactor, FactorContext, FactorResult


class MoneyflowMainNetFactor(BaseFactor):
    name = "moneyflow_main_net"
    description = "主力净流入率 (main_net_inflow / 成交额)"
    category = "sentiment"
    window_size = 5

    def compute(self, ctx: FactorContext) -> FactorResult:
        if not ctx.moneyflow:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        latest = ctx.moneyflow[-1]
        main_net = float(getattr(latest, "main_net_inflow", 0) or 0)
        score = max(-1.0, min(1.0, main_net / 1e8))
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=round(score, 4), raw_value=main_net,
        )