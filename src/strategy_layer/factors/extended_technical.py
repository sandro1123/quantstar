"""扩展技术指标因子 — Stochastic %K / CCI / ADX / Williams %R / OBV Trend"""
from strategy_layer.factors._utils import ema, extract, rolling_max, rolling_mean, rolling_min
from strategy_layer.factors.base import BaseFactor, FactorContext, FactorResult


class StochasticKFactor(BaseFactor):
    name = "stochastic_k"
    description = "随机指标%K(14日)"
    category = "technical"
    window_size = 15

    def compute(self, ctx: FactorContext) -> FactorResult:
        closes = extract(ctx.klines, "close")
        if len(closes) < self.window_size:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        highs = extract(ctx.klines, "high")
        lows = extract(ctx.klines, "low")
        hh = rolling_max(highs, 14)
        ll = rolling_min(lows, 14)
        rng = hh[-1] - ll[-1]
        pct_k = (closes[-1] - ll[-1]) / rng * 100 if rng > 0 else 0.0
        score = pct_k / 100.0
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=score, raw_value=pct_k,
            details={"pct_k": round(pct_k, 2), "highest_high": round(hh[-1], 2),
                     "lowest_low": round(ll[-1], 2)},
        )


class CCIFactor(BaseFactor):
    name = "cci"
    description = "CCI(20日)"
    category = "technical"
    window_size = 25

    def compute(self, ctx: FactorContext) -> FactorResult:
        closes = extract(ctx.klines, "close")
        if len(closes) < self.window_size:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        highs = extract(ctx.klines, "high")
        lows = extract(ctx.klines, "low")
        tp = [(h + lo + c) / 3.0 for h, lo, c in zip(highs, lows, closes)]
        sma_tp = rolling_mean(tp, 20)
        abs_dev = [abs(tp[i] - sma_tp[i]) if i >= 19 else 0.0 for i in range(len(tp))]
        mad = rolling_mean(abs_dev, 20)
        denominator = 0.015 * mad[-1]
        cci = (tp[-1] - sma_tp[-1]) / denominator if denominator > 0 else 0.0
        score = cci / 200.0
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=score, raw_value=cci,
            details={"cci": round(cci, 2), "tp": round(tp[-1], 2),
                     "sma_tp": round(sma_tp[-1], 2)},
        )


class ADXFactor(BaseFactor):
    name = "adx"
    description = "ADX趋势强度(14日)"
    category = "technical"
    window_size = 30

    def compute(self, ctx: FactorContext) -> FactorResult:
        closes = extract(ctx.klines, "close")
        if len(closes) < self.window_size:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        highs = extract(ctx.klines, "high")
        lows = extract(ctx.klines, "low")
        n = len(closes)
        tr_list, plus_dm, minus_dm = [0.0] * n, [0.0] * n, [0.0] * n
        for i in range(1, n):
            hl = highs[i] - lows[i]
            hc = abs(highs[i] - closes[i - 1])
            lc = abs(lows[i] - closes[i - 1])
            tr_list[i] = max(hl, hc, lc)
            up = highs[i] - highs[i - 1]
            dn = lows[i - 1] - lows[i]
            plus_dm[i] = up if (up > dn and up > 0) else 0.0
            minus_dm[i] = dn if (dn > up and dn > 0) else 0.0
        atr = ema(tr_list[1:], 14)
        sm_plus = ema(plus_dm[1:], 14)
        sm_minus = ema(minus_dm[1:], 14)
        dx_list = []
        for i in range(len(atr)):
            denom = sm_plus[i] + sm_minus[i]
            if denom > 0:
                dx_list.append(abs(sm_plus[i] - sm_minus[i]) / denom * 100.0)
            else:
                dx_list.append(0.0)
        adx_vals = ema(dx_list, 14)
        adx = adx_vals[-1]
        score = adx / 100.0
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=score, raw_value=adx,
            details={"adx": round(adx, 2)},
        )


class WilliamsRFactor(BaseFactor):
    name = "williams_r"
    description = "威廉%R(14日)"
    category = "technical"
    window_size = 15

    def compute(self, ctx: FactorContext) -> FactorResult:
        closes = extract(ctx.klines, "close")
        if len(closes) < self.window_size:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        highs = extract(ctx.klines, "high")
        lows = extract(ctx.klines, "low")
        hh = rolling_max(highs, 14)
        ll = rolling_min(lows, 14)
        rng = hh[-1] - ll[-1]
        wr = (hh[-1] - closes[-1]) / rng * -100.0 if rng > 0 else 0.0
        score = (wr + 100.0) / 100.0
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=score, raw_value=wr,
            details={"williams_r": round(wr, 2)},
        )


class OBVFactor(BaseFactor):
    name = "obv_trend"
    description = "OBV趋势(20日)"
    category = "technical"
    window_size = 25

    def compute(self, ctx: FactorContext) -> FactorResult:
        closes = extract(ctx.klines, "close")
        if len(closes) < self.window_size:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)
        volumes = extract(ctx.klines, "volume")
        n = len(closes)
        obv = [0.0] * n
        for i in range(1, n):
            if closes[i] > closes[i - 1]:
                obv[i] = obv[i - 1] + volumes[i]
            elif closes[i] < closes[i - 1]:
                obv[i] = obv[i - 1] - volumes[i]
            else:
                obv[i] = obv[i - 1]
        obv_window = obv[-20:]
        delta = obv[-1] - obv[-20]
        denom = max(abs(v) for v in obv_window) + 1e-8
        score = delta / denom
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=score, raw_value=delta,
            details={"obv_current": round(obv[-1], 2), "obv_20ago": round(obv[-20], 2),
                     "obv_trend": round(delta, 2)},
        )
