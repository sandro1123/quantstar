"""概念板块因子 — 基于 concept_daily 表的因子"""
from strategy_layer.factors.base import BaseFactor, FactorContext, FactorResult


class ConceptCountFactor(BaseFactor):
    """概念数量因子: 股票所属概念板块数量"""
    name = "concept_count"
    description = "股票所属概念板块数量"
    category = "sentiment"

    def compute(self, ctx: FactorContext) -> FactorResult:
        if not ctx.concept_data:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=0.0)
        unique_concepts = set()
        for item in ctx.concept_data:
            name = item.get("concept_name", "")
            if name:
                unique_concepts.add(name)
        count = len(unique_concepts)
        score = min(1.0, count / 10.0)
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=round(score, 4), raw_value=float(count),
        )


class ConceptHeatFactor(BaseFactor):
    """概念热度因子: 概念板块当日涨幅"""
    name = "concept_heat"
    description = "概念板块当日涨幅 (通过 concept_daily 计算)"
    category = "sentiment"
    window_size = 1

    def compute(self, ctx: FactorContext) -> FactorResult:
        if not ctx.concept_data:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=0.0)
        latest = ctx.concept_data[-1]
        change_pct = latest.get("change_pct", 0) or 0
        score = max(-1.0, min(1.0, change_pct / 5.0))
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=round(score, 4), raw_value=float(change_pct),
        )
