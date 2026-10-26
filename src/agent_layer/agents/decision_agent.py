"""DecisionAgent — LLM + 数据聚合 → 综合推荐"""
from agent_layer._models import AgentResult
from agent_layer.agents.base import BaseAgent
from agent_layer.llm.structured_output import parse_decision_via_instructor
from common.logging import get_logger

logger = get_logger("agent.decision")

SYSTEM_PROMPT = """你是A股量化选股分析助手。根据提供的因子评分和市场状态，给出股票推荐。
只输出严格 JSON（不要任何其他文字）:
{"verdict": "推荐/不推荐/观望", "confidence": 0.0-1.0, "reasoning": "简短理由", "risk_flags": ["风险点"]}"""


class DecisionAgent(BaseAgent):
    """LLM 驱动的综合决策 Agent"""

    def __init__(self, provider_manager=None, prompt_manager=None):
        self._pm = provider_manager
        self._prompt_manager = prompt_manager

    def analyze(self, data: dict) -> AgentResult:
        code = data.get("code", "")
        factor_scores = data.get("factor_scores", {})
        market_regime = data.get("market_regime", "neutral")
        analysis_summary = data.get("analysis_summary", "")

        if not self._pm or not self._pm.has_available_llm():
            return self._rule_based(data)

        prompt = self._build_prompt(code, factor_scores, market_regime, analysis_summary)

        call_point = f"decision:{code}"
        # P0-6: instructor 结构化输出 — pydantic 校验 + 校验错误回灌重试,
        # 传输仍走既有 9router 全链 (见 llm/structured_output.py);
        # instructor 未安装或重试耗尽时返回 None → 落到下方 legacy 兜底。
        verdict = parse_decision_via_instructor(
            self._pm, prompt=prompt, system_prompt=SYSTEM_PROMPT,
            call_point=call_point, temperature=0.2, max_tokens=800)
        if verdict is not None:
            used_prov = self._pm.client.last_used_provider if self._pm else ""
            return AgentResult(
                verdict=verdict.verdict,
                confidence=verdict.confidence,
                reasoning=verdict.reasoning or "LLM分析",
                risk_flags=verdict.risk_flags,
                quality={"model_used": used_prov or "llm", "tier_used": "llm",
                         "parser": "instructor"},
            )

        # 兜底: 既有裸调用 + regex/行解析 (instructor 未安装时的原路径)
        try:
            result = self._pm.call(
                prompt=prompt,
                system_prompt=SYSTEM_PROMPT,
                call_point=call_point,
                temperature=0.2,
                max_tokens=800,
            )
            if result:
                parsed = self._parse_response(result)
                if parsed:
                    # Q2: 留痕用了哪一级 provider
                    used_prov = self._pm.client.last_used_provider if self._pm else ""
                    return AgentResult(
                        verdict=parsed.get("verdict", "观望"),
                        confidence=float(parsed.get("confidence", 0.5)),
                        reasoning=parsed.get("reasoning", "LLM分析"),
                        risk_flags=parsed.get("risk_flags", []),
                        quality={"model_used": used_prov or "llm", "tier_used": "llm",
                                 "parser": "legacy"},
                    )
        except Exception as e:
            logger.warning(f"DecisionAgent LLM call failed: {e}")

        return self._rule_based(data)

    def _build_prompt(self, code: str, factor_scores: dict, market_regime: str,
                      analysis_summary: str) -> str:
        if self._prompt_manager:
            # AG-GUARD 20260907: 全精度 str() 小数位易拼 11/18 位连续数字串,
            # 与 guardrail 边界断言双保险, 且省 token
            scores_r = {k: round(v, 6) for k, v in factor_scores.items()} \
                if isinstance(factor_scores, dict) else factor_scores
            template = self._prompt_manager.load(
                "decision_recommend",
                code=code,
                name=code,
                factor_scores=str(scores_r),
                market_regime=market_regime,
                analysis_summary=analysis_summary[:500],
            )
            if template:
                return template
        lines = [f"股票: {code}", f"市场状态: {market_regime}"]
        if factor_scores:
            top_factors = sorted(factor_scores.items(), key=lambda x: abs(x[1]), reverse=True)[:10]
            lines.append("因子评分 (Top 10):")
            for name, val in top_factors:
                lines.append(f"  {name}: {val:.3f}")
            avg = sum(factor_scores.values()) / len(factor_scores)
            lines.append(f"  综合均值: {avg:.3f}")
        if analysis_summary:
            lines.append(f"补充信息: {analysis_summary[:500]}")
        lines.append("\n请分析并给出推荐。")
        return "\n".join(lines)

    def _parse_response(self, text: str) -> dict | None:
        # JSON 优先 (prompt_manager 模板指示 JSON 输出)
        try:
            import json as _json
            import re as _re
            m = _re.search(r'\{[\s\S]*\}', text)
            if m:
                data = _json.loads(m.group())
                if "verdict" in data and "confidence" in data:
                    data.setdefault("reasoning", "LLM分析")
                    data.setdefault("risk_flags", [])
                    if isinstance(data.get("risk_flags"), str):
                        data["risk_flags"] = [f.strip() for f in data["risk_flags"].split(",") if f.strip()]
                    return data
        except Exception:
            pass
        # 行格式兜底 (SYSTEM_PROMPT 指示的 verdict:/confidence: 格式)
        try:
            parsed = {}
            for line in text.strip().split("\n"):
                line = line.strip()
                if line.lower().startswith("verdict:"):
                    parsed["verdict"] = line.split(":", 1)[1].strip()
                elif line.lower().startswith("confidence:"):
                    parsed["confidence"] = float(line.split(":", 1)[1].strip())
                elif line.lower().startswith("reasoning:"):
                    parsed["reasoning"] = line.split(":", 1)[1].strip()
                elif line.lower().startswith("risk_flags:"):
                    flags_str = line.split(":", 1)[1].strip()
                    parsed["risk_flags"] = [f.strip() for f in flags_str.split(",") if f.strip()]
            if "verdict" in parsed and "confidence" in parsed:
                parsed.setdefault("reasoning", "LLM分析")
                parsed.setdefault("risk_flags", [])
                return parsed
        except Exception:
            pass
        return None

    def _rule_based(self, data: dict) -> AgentResult:
        factor_scores = data.get("factor_scores", {})
        composite = sum(factor_scores.values()) / max(len(factor_scores), 1) if factor_scores else 0
        if composite > 0.5:
            verdict, confidence = "推荐", min(composite, 0.8)
        elif composite < -0.5:
            verdict, confidence = "不推荐", min(abs(composite), 0.8)
        else:
            verdict, confidence = "观望", 0.5
        return AgentResult(
            verdict=verdict, confidence=confidence,
            reasoning=f"规则模式: composite_score={composite:.2f}「未调LLM」",
            quality={"model_used": "rule", "tier_used": "rule_fallback"},
        )
