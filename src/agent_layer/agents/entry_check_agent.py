"""入场审核 Agent — 纯规则 (AG-03: verdict 统一词表 推荐/不推荐/观望/警惕)

语义: 推荐=审核通过, 不推荐=审核拒绝。时段异常只记风险标记不改变判定。
"""
from datetime import datetime, timedelta, timezone

from agent_layer._models import AgentResult
from agent_layer.agents.base import BaseAgent

BJ_TZ = timezone(timedelta(hours=8))

ST_KEYWORDS = ("ST", "退", "*ST")


class EntryCheckAgent(BaseAgent):
    name = "entry_check"

    def analyze(self, input_data: dict) -> AgentResult:
        code = input_data.get("code", "")
        entry_price = input_data.get("entry_price", 0)
        risk_flags: list[str] = []
        verdict = "推荐"  # 默认通过

        if any(kw in code for kw in ST_KEYWORDS):
            risk_flags.append(f"code_blacklist:{code} 含 ST/退市关键词")
            verdict = "不推荐"

        if not isinstance(entry_price, (int, float)) or not (0 < entry_price < 1000):
            risk_flags.append(f"price_range: {entry_price} 超出 0-1000")
            verdict = "不推荐"

        now = datetime.now(BJ_TZ)
        h, m = now.hour, now.minute
        in_session = (9 <= h < 15) and not (h == 14 and m > 50)
        if not in_session:
            risk_flags.append(f"trading_hours: {now.strftime('%H:%M')} 不在 09:30-14:50")

        return AgentResult(
            verdict=verdict,
            confidence=1.0 if verdict == "推荐" else 0.0,
            reasoning="; ".join(risk_flags) if risk_flags else "入场审核通过",
            risk_flags=risk_flags,
        )
