"""龙虎榜因子 — 基于 lhb_daily 表的因子"""
from strategy_layer.factors.base import BaseFactor, FactorContext, FactorResult


class LhbNetBuyFactor(BaseFactor):
    """龙虎榜净买入因子: 龙虎榜净买入金额"""
    name = "lhb_net_buy"
    description = "龙虎榜净买入金额"
    category = "sentiment"

    def compute(self, ctx: FactorContext) -> FactorResult:
        if not ctx.lhb_data:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=0.0)
        latest = ctx.lhb_data[-1]
        net_buy = latest.get("net_buy_amount", 0) or 0
        score = max(-1.0, min(1.0, net_buy / 1e8))
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=round(score, 4), raw_value=float(net_buy),
        )


class LhbDramaScoreFactor(BaseFactor):
    """龙虎榜异动因子: lhb_drama_score"""
    name = "lhb_drama_score"
    description = "龙虎榜异动评分"
    category = "sentiment"

    def compute(self, ctx: FactorContext) -> FactorResult:
        if not ctx.lhb_data:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=0.0)
        latest = ctx.lhb_data[-1]
        drama = latest.get("drama_score", 0) or 0
        score = max(0.0, min(1.0, drama / 100.0))
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=round(score, 4), raw_value=float(drama),
        )


class AnalystConsensusFactor(BaseFactor):
    """分析师共识因子: analyst_consensus_score"""
    name = "analyst_consensus"
    description = "分析师共识评分"
    category = "sentiment"

    def compute(self, ctx: FactorContext) -> FactorResult:
        if not ctx.analyst_data:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=0.0)
        latest = ctx.analyst_data[-1]
        consensus = latest.get("consensus_score", 0) or 0
        score = max(-1.0, min(1.0, consensus))
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=round(score, 4), raw_value=float(consensus),
        )
