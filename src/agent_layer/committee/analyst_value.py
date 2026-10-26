"""agent_layer/committee/analyst_value.py — ValueAnalyst: 价值投资视角"""


class ValueAnalyst:
    name = "value"

    def analyze(self, code: str, factor_scores: dict) -> dict:
        pe = factor_scores.get("pe", 30)
        pb = factor_scores.get("pb", 2)
        roe = factor_scores.get("roe", 10)
        score = 0.0
        if 5 < pe < 20:
            score += 0.3
        if pb < 1.5:
            score += 0.3
        if roe > 15:
            score += 0.4
        confidence = 0.7
        verdict = "推荐" if score > 0.5 else ("观望" if score > 0 else "不推荐")
        return {"analyst": self.name, "verdict": verdict, "score": round(score, 2),
                "confidence": confidence, "reasoning": f"PE={pe}, PB={pb}, ROE={roe}%"}


class MomentumAnalyst:
    name = "momentum"

    def analyze(self, code: str, factor_scores: dict) -> dict:
        momentum_20d = factor_scores.get("momentum_20d", 0)
        volume_change = factor_scores.get("volume_change", 0)
        score = 0.0
        if momentum_20d > 5:
            score += 0.4
        elif momentum_20d < -5:
            score -= 0.4
        if volume_change > 10:
            score += 0.2
        elif volume_change < -10:
            score -= 0.2
        confidence = 0.6
        verdict = "推荐" if score > 0.3 else ("观望" if score > -0.3 else "不推荐")
        return {"analyst": self.name, "verdict": verdict, "score": round(score, 2),
                "confidence": confidence, "reasoning": f"20日动量={momentum_20d}%, 量变={volume_change}%"}


class RiskAnalyst:
    name = "risk"

    def analyze(self, code: str, factor_scores: dict) -> dict:
        pe = factor_scores.get("pe", 30)
        debt_ratio = factor_scores.get("debt_ratio", 50)
        volatility = factor_scores.get("volatility", 0.3)
        score = 0.0
        if pe > 100 or pe < 0:
            score -= 0.3
        if debt_ratio > 70:
            score -= 0.3
        if volatility > 0.5:
            score -= 0.2
        elif volatility < 0.2:
            score += 0.2
        confidence = 0.7
        verdict = "推荐" if score > 0.2 else ("观望" if score > -0.3 else "不推荐")
        return {"analyst": self.name, "verdict": verdict, "score": round(score, 2),
                "confidence": confidence, "reasoning": f"PE={pe}, 负债率={debt_ratio}%, 波动率={volatility}"}


class TechnicalAnalyst:
    name = "technical"

    def analyze(self, code: str, factor_scores: dict) -> dict:
        rsi = factor_scores.get("rsi", 50)
        macd = factor_scores.get("macd", 0)
        score = 0.0
        if rsi < 30:
            score += 0.3
        elif rsi > 70:
            score -= 0.3
        if macd > 0:
            score += 0.3
        elif macd < 0:
            score -= 0.3
        confidence = 0.6
        verdict = "推荐" if score > 0.3 else ("观望" if score > -0.3 else "不推荐")
        return {"analyst": self.name, "verdict": verdict, "score": round(score, 2),
                "confidence": confidence, "reasoning": f"RSI={rsi}, MACD={macd}"}


class FundamentalAnalyst:
    name = "fundamental"

    def analyze(self, code: str, factor_scores: dict) -> dict:
        revenue_growth = factor_scores.get("revenue_growth", 5)
        profit_growth = factor_scores.get("profit_growth", 5)
        roe = factor_scores.get("roe", 10)
        score = 0.0
        if revenue_growth > 10:
            score += 0.3
        elif revenue_growth < 0:
            score -= 0.3
        if profit_growth > 10:
            score += 0.3
        elif profit_growth < 0:
            score -= 0.3
        if roe > 15:
            score += 0.2
        confidence = 0.6
        verdict = "推荐" if score > 0.4 else ("观望" if score > -0.2 else "不推荐")
        return {"analyst": self.name, "verdict": verdict, "score": round(score, 2),
                "confidence": confidence,
                "reasoning": f"营收增长={revenue_growth}%, 利润增长={profit_growth}%, ROE={roe}%"}


class SentimentAnalyst:
    name = "sentiment"

    def analyze(self, code: str, factor_scores: dict) -> dict:
        turnover_ratio = factor_scores.get("turnover_ratio", 3)
        sentiment_score = factor_scores.get("sentiment_score", 0)
        score = 0.0
        if turnover_ratio > 8:
            score += 0.2
        elif turnover_ratio < 1:
            score -= 0.1
        if sentiment_score > 0.3:
            score += 0.3
        elif sentiment_score < -0.3:
            score -= 0.3
        confidence = 0.5
        verdict = "推荐" if score > 0.3 else ("观望" if score > -0.3 else "不推荐")
        return {"analyst": self.name, "verdict": verdict, "score": round(score, 2),
                "confidence": confidence,
                "reasoning": f"换手率={turnover_ratio}%, 情绪分={sentiment_score}"}


class QuantAnalyst:
    name = "quant"

    def analyze(self, code: str, factor_scores: dict) -> dict:
        ic = factor_scores.get("ic", 0.05)
        alpha = factor_scores.get("alpha", 0)
        score = 0.0
        if ic > 0.1:
            score += 0.4
        elif ic < -0.1:
            score -= 0.4
        if alpha > 0.05:
            score += 0.3
        elif alpha < -0.05:
            score -= 0.3
        confidence = 0.5
        verdict = "推荐" if score > 0.4 else ("观望" if score > -0.3 else "不推荐")
        return {"analyst": self.name, "verdict": verdict, "score": round(score, 2),
                "confidence": confidence, "reasoning": f"IC={ic}, Alpha={alpha}"}
