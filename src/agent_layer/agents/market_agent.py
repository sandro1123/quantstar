"""MarketAgent — 市场级分析 (AG-11)

从 DataLayer 真实数据构建市场研判:
- 涨跌家数/市场温度 (MarketState)
- 涨停/跌停/情绪 (SentimentData)
- 量能对比 (今日 vs 前一交易日 total amount)
- 规则判定 + 可选 LLM 叙述增强

verdict 遵循统一词表 (AG-03): 推荐/不推荐/观望/警惕。
市场级语义: 推荐=看多, 不推荐=看空, 观望=震荡无方向。

数据边界: index_daily/北向资金当前不存在 (DL-23 待建), 不伪造。
"""
from agent_layer._models import AgentResult
from agent_layer.agents.base import BaseAgent
from common.logging import get_logger

logger = get_logger("agent.market")


class MarketAgent(BaseAgent):
    """市场级分析 Agent — 规则核心 + LLM 可选增强"""
    name = "market_agent"

    def __init__(self, data_layer=None, provider_manager=None):
        self._data = data_layer
        self._pm = provider_manager

    def analyze(self, input_data: dict) -> AgentResult:
        date = input_data.get("date", "")
        if not self._data:
            return AgentResult(verdict="观望", confidence=0.3,
                               reasoning="DataLayer 未接入",
                               quality={"model_used": "rule", "tier_used": "rule_fallback",
                                        "latency_ms": 0, "providers_tried": []})

        # 1. 市场广度
        breadth = self._get_breadth(date)
        # 2. 情绪 (涨跌停)
        sentiment = self._get_sentiment(date)
        # 3. 量能对比
        volume_signal = self._get_volume_signal(date)

        # 规则判定
        score = 0.0
        reasons = []

        if breadth:
            temp = breadth.get("market_temperature", 0)
            up = breadth.get("up_count", 0)
            down = breadth.get("down_count", 0)
            total = up + down
            if total > 0:
                up_ratio = up / total
                if up_ratio > 0.6:
                    score += 1.0
                    reasons.append(f"涨多跌少({up}/{down})")
                elif up_ratio < 0.4:
                    score -= 1.0
                    reasons.append(f"跌多涨少({up}/{down})")
            if temp > 1.5:
                score += 0.5
                reasons.append(f"市场温度偏高({temp:+.1f}%)")
            elif temp < -1.5:
                score -= 0.5
                reasons.append(f"市场温度偏低({temp:+.1f}%)")

        if sentiment:
            zt = sentiment.get("zt_count", 0)
            dt = sentiment.get("dt_count", 0)
            if zt > 50 and dt < 10:
                score += 0.5
                reasons.append(f"涨停活跃({zt}家)")
            elif dt > 30 and zt < 10:
                score -= 0.5
                reasons.append(f"跌停恐慌({dt}家)")

        if volume_signal is not None:
            if volume_signal > 1.3:
                reasons.append(f"放量({volume_signal:.1f}x)")
                score += 0.3 if score >= 0 else -0.3  # 放量顺势
            elif volume_signal < 0.7:
                reasons.append(f"缩量({volume_signal:.1f}x)")

        # 判定 — 统一词表: 推荐(看多)/不推荐(看空)/观望(震荡)
        if score >= 1.0:
            verdict, confidence = "推荐", min(0.5 + score * 0.15, 0.85)
        elif score <= -1.0:
            verdict, confidence = "不推荐", min(0.5 + abs(score) * 0.15, 0.85)
        else:
            verdict, confidence = "观望", 0.5 + abs(score) * 0.1

        reasoning = "市场研判: " + "; ".join(reasons) if reasons else "市场信号中性, 无明确方向"

        result = AgentResult(
            verdict=verdict, confidence=round(confidence, 2),
            reasoning=reasoning,
            output={
                "breadth": breadth,
                "sentiment": sentiment,
                "volume_ratio": volume_signal,
                "score": round(score, 2),
                "position_pct": self._suggest_position(score),
            },
            quality={"model_used": "rule", "tier_used": "market_rule",
                     "latency_ms": 0, "providers_tried": []},
        )

        # LLM 叙述增强 (可选, 失败不影响规则判定)
        llm_narrative = self._llm_enhance(date, verdict, reasoning)
        if llm_narrative:
            result.reasoning += f"\n🧠 {llm_narrative}"
            result.quality = {"model_used": "llm_enhanced", "tier_used": "market_rule+llm",
                              "latency_ms": 0, "providers_tried": []}

        return result

    def _get_breadth(self, date: str) -> dict | None:
        try:
            snap = self._data.snapshot(date)
            if not snap or not snap.market:
                return None
            m = snap.market
            return {
                "state": m.state,
                "up_count": m.up_count,
                "down_count": m.down_count,
                "market_temperature": m.market_temperature,
            }
        except Exception:
            return None

    def _get_sentiment(self, date: str) -> dict | None:
        try:
            s = self._data.get_sentiment_data(date)
            if not s:
                return None
            return {
                "zt_count": s.zt_count,
                "dt_count": s.dt_count,
                "limit_up_count": s.limit_up_count,
                "limit_down_count": s.limit_down_count,
                "market_temp": s.market_temp,
            }
        except Exception:
            return None

    def _get_volume_signal(self, date: str) -> float | None:
        """今日总成交额 / 前一交易日总成交额"""
        try:
            snaps = self._data.snapshot_range(date, date)
            if not snaps:
                return None
            today_amount = sum(k.amount for k in snaps[0].klines) if snaps[0].klines else 0
            if today_amount <= 0:
                return None
            from datetime import datetime, timedelta
            dt = datetime.strptime(date, "%Y-%m-%d")
            for back in range(1, 8):
                prev_date = (dt - timedelta(days=back)).strftime("%Y-%m-%d")
                prev_snaps = self._data.snapshot_range(prev_date, prev_date)
                if prev_snaps and prev_snaps[0].klines:
                    prev_amount = sum(k.amount for k in prev_snaps[0].klines)
                    if prev_amount > 0:
                        return round(today_amount / prev_amount, 2)
            return None
        except Exception:
            return None

    def _suggest_position(self, score: float) -> float:
        """规则仓位建议: score [-2,2] → position [0.2, 0.8]"""
        return round(max(0.2, min(0.8, 0.5 + score * 0.15)), 2)

    def _llm_enhance(self, date: str, verdict: str, reasoning: str) -> str | None:
        """可选 LLM 叙述 — 失败返回 None, 不影响规则判定"""
        if not self._pm or not self._pm.has_available_llm():
            return None
        try:
            prompt = (
                f"今日({date})A股市场研判: {verdict}。{reasoning}。\n"
                f"请用1-2句话补充市场解读要点，不要markdown。"
            )
            return self._pm.call(prompt=prompt, call_point=f"market_analysis:{date}",
                                 temperature=0.5, max_tokens=2000)
        except Exception:
            return None
