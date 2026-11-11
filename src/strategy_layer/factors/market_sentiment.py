"""市场情绪因子 — 基于涨跌分布/涨停数据的市场温度因子"""
from strategy_layer.factors.base import BaseFactor, FactorContext, FactorResult


class MarketSentimentFactor(BaseFactor):
    name = "market_sentiment"
    description = "市场情绪温度 (涨停数/跌停数/涨跌比)"
    category = "sentiment"
    window_size = 1

    def compute(self, ctx: FactorContext) -> FactorResult:
        if not ctx.sentiment_data:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        zt = float(ctx.sentiment_data.get("zt_count", 0) or 0)
        dt = float(ctx.sentiment_data.get("dt_count", 0) or 0)
        up = float(ctx.sentiment_data.get("up_count", 0) or 0)
        down = float(ctx.sentiment_data.get("down_count", 0) or 0)
        total = up + down
        if total == 0:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        up_ratio = up / total
        score = (up_ratio - 0.5) * 2 + (zt - dt) / max(total, 100) * 10
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=round(score, 4), raw_value=float(zt / max(dt, 1)),
        )