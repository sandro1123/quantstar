"""形态因子: K 线形态与均线交叉检测"""
from strategy_layer.factors.base import BaseFactor, FactorContext, FactorResult


class PatternFactor(BaseFactor):
    name = "pattern_technical"
    description = "K线形态（锤子/流星/金叉/死叉）"
    category = "technical"
    window_size = 200

    def compute(self, ctx: FactorContext) -> FactorResult:
        if len(ctx.klines) < 5:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)

        klines = ctx.klines
        last = klines[-1]
        open_p = last["open"] if isinstance(last, dict) else last.open
        close = last["close"] if isinstance(last, dict) else last.close
        high = last["high"] if isinstance(last, dict) else last.high
        low = last["low"] if isinstance(last, dict) else last.low

        body = abs(close - open_p)
        total_range = high - low if high != low else 1
        lower_wick = min(open_p, close) - low
        upper_wick = high - max(open_p, close)

        score = 0.0
        patterns = []
        details = {}

        # Hammer: long lower wick, small body near top
        if body < total_range * 0.3 and lower_wick > body * 2 and lower_wick > upper_wick * 2:
            score += 1.0
            patterns.append("hammer")
            details["hammer"] = True

        # Shooting star: long upper wick, small body near bottom
        if body < total_range * 0.3 and upper_wick > body * 2 and upper_wick > lower_wick * 2:
            score -= 1.0
            patterns.append("shooting_star")
            details["shooting_star"] = True

        # Golden cross / Death cross using SMA approximations
        if len(klines) >= 200:
            closes = [k["close"] if isinstance(k, dict) else k.close for k in klines]
            sma50 = sum(closes[-50:]) / 50
            sma50_prev = sum(closes[-51:-1]) / 50
            sma200 = sum(closes[-200:]) / 200
            sma200_prev = sum(closes[-201:-1]) / 200 if len(closes) > 200 else sma200

            if sma50_prev <= sma200_prev and sma50 > sma200:
                score += 2.0
                patterns.append("golden_cross")
                details["golden_cross"] = True
            elif sma50_prev >= sma200_prev and sma50 < sma200:
                score -= 2.0
                patterns.append("death_cross")
                details["death_cross"] = True

        details["patterns"] = patterns
        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=score, raw_value=score,
            details=details,
        )