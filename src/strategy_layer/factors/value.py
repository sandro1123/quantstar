"""价值因子: PE/PB/PS 综合折价"""
from strategy_layer.factors.base import BaseFactor, FactorContext, FactorResult, is_valid_positive


class ValueFactor(BaseFactor):
    name = "value_discount"
    description = "PE/PB/PS 估值折价（低估值高分）"
    category = "value"
    window_size = 1

    def compute(self, ctx: FactorContext) -> FactorResult:
        missing = FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name, score=None, raw_value=None)
        if not ctx.valuations:
            return missing
        v = ctx.valuations[-1]
        if isinstance(v, dict):
            pe, pb, ps = v.get("pe_ttm"), v.get("pb_mrq"), v.get("ps_ttm")
        else:
            pe = getattr(v, "pe_ttm", None)
            pb = getattr(v, "pb_mrq", None)
            ps = getattr(v, "ps_ttm", None)
        # 低估值 → 高分: 取负数；缺失/非法字段不参与合成
        vals = []
        used = []
        missing_components = []
        for name, val in (("pe_ttm", pe), ("pb_mrq", pb), ("ps_ttm", ps)):
            if is_valid_positive(val):
                vals.append(-val)
                used.append(name)
            else:
                missing_components.append(name)

        if not vals:
            return FactorResult(
                code=ctx.code, date=ctx.date, factor_name=self.name,
                score=None, raw_value=None,
                details={"pe": pe, "pb": pb, "ps": ps, "used": used, "missing": missing_components},
            )
        score = sum(vals) / len(vals)
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=score, raw_value=score,
            details={"pe": pe, "pb": pb, "ps": ps, "used": used, "missing": missing_components},
        )
