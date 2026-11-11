"""扩展流动性因子: Amihud非流动性 / 平均换手率 / 成交量趋势 / 流动性动量 / 平均振幅"""
from strategy_layer.factors._utils import extract
from strategy_layer.factors.base import BaseFactor, FactorContext, FactorResult


class AmihudIlliquidityFactor(BaseFactor):
    """Amihud非流动性指标(20日): mean(|return| / volume). 越低流动性越好 → score取负"""
    name = "amihud_illiquidity"
    description = "Amihud非流动性(20日)"
    category = "liquidity"
    window_size = 25

    def compute(self, ctx: FactorContext) -> FactorResult:
        try:
            klines = ctx.klines
            if len(klines) < 21:
                return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
            closes = extract(klines, "close")
            volumes = extract(klines, "volume")
            ratios = []
            for i in range(len(klines) - 20, len(klines)):
                if i < 1:
                    continue
                vol = volumes[i]
                if not vol or vol <= 0:
                    continue
                ret = abs(closes[i] / closes[i - 1] - 1)
                ratios.append(ret / vol)
            if not ratios:
                return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
            amihud = sum(ratios) / len(ratios)
            return FactorResult(
                code=ctx.code, date=ctx.date, factor_name=self.name,
                score=-amihud, raw_value=amihud,
                details={"amihud_illiquidity_20d": amihud},
            )
        except Exception:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)


class TurnoverRateFactor(BaseFactor):
    """20日平均换手率. 换手率越高流动性越好 → 分数越高"""
    name = "avg_turnover_rate"
    description = "20日平均换手率"
    category = "liquidity"
    window_size = 25

    def compute(self, ctx: FactorContext) -> FactorResult:
        try:
            klines = ctx.klines
            if len(klines) < 20:
                return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
            turnover_values = extract(klines, "turnover_rate")[-20:]
            valid = [t for t in turnover_values if t is not None and t > 0]
            if not valid:
                return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
            avg_tr = sum(valid) / len(valid)
            return FactorResult(
                code=ctx.code, date=ctx.date, factor_name=self.name,
                score=avg_tr, raw_value=avg_tr,
                details={"avg_turnover_rate_20d": avg_tr},
            )
        except Exception:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)


class VolumeTrendFactor(BaseFactor):
    """成交量趋势: mean(vol[-5:]) / mean(vol[-20:]) - 1. 正值表示放量扩张"""
    name = "volume_trend"
    description = "成交量趋势(5/20日)"
    category = "liquidity"
    window_size = 25

    def compute(self, ctx: FactorContext) -> FactorResult:
        try:
            klines = ctx.klines
            if len(klines) < 20:
                return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
            volumes = extract(klines, "volume")
            vol_short = volumes[-5:]
            vol_long = volumes[-20:]
            avg_short = sum(vol_short) / len(vol_short)
            avg_long = sum(vol_long) / len(vol_long)
            if avg_long <= 0:
                return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
            score = avg_short / avg_long - 1
            return FactorResult(
                code=ctx.code, date=ctx.date, factor_name=self.name,
                score=score, raw_value=score,
                details={"vol_5d": avg_short, "vol_20d": avg_long, "trend": score},
            )
        except Exception:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)


class LiquidityMomentumFactor(BaseFactor):
    """流动性动量: mean(turnover[-5:]) / mean(turnover[-20:]) - 1. 换手率上升→分数更高"""
    name = "liquidity_momentum"
    description = "流动性动量(换手率趋势)"
    category = "liquidity"
    window_size = 25

    def compute(self, ctx: FactorContext) -> FactorResult:
        try:
            klines = ctx.klines
            if len(klines) < 20:
                return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
            turnover = extract(klines, "turnover_rate")
            tr_short = turnover[-5:]
            tr_long = turnover[-20:]
            valid_short = [t for t in tr_short if t is not None and t > 0]
            valid_long = [t for t in tr_long if t is not None and t > 0]
            if not valid_short or not valid_long:
                return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
            avg_short = sum(valid_short) / len(valid_short)
            avg_long = sum(valid_long) / len(valid_long)
            if avg_long <= 0:
                return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
            score = avg_short / avg_long - 1
            return FactorResult(
                code=ctx.code, date=ctx.date, factor_name=self.name,
                score=score, raw_value=score,
                details={"tr_5d": avg_short, "tr_20d": avg_long, "momentum": score},
            )
        except Exception:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)


class HighLowSpreadFactor(BaseFactor):
    """20日平均振幅: mean((high-low)/close). 振幅越小流动性越好 → score取负"""
    name = "avg_hl_spread"
    description = "20日平均振幅"
    category = "liquidity"
    window_size = 25

    def compute(self, ctx: FactorContext) -> FactorResult:
        try:
            klines = ctx.klines
            if len(klines) < 20:
                return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
            highs = extract(klines, "high")
            lows = extract(klines, "low")
            closes = extract(klines, "close")
            spreads = []
            for i in range(len(klines) - 20, len(klines)):
                c = closes[i]
                if not c or c <= 0:
                    continue
                spreads.append((highs[i] - lows[i]) / c)
            if not spreads:
                return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
            avg_spread = sum(spreads) / len(spreads)
            return FactorResult(
                code=ctx.code, date=ctx.date, factor_name=self.name,
                score=-avg_spread, raw_value=avg_spread,
                details={"avg_hl_spread_20d": avg_spread},
            )
        except Exception:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
