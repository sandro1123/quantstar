"""个股精选 Section — 含选股逻辑解释

模块1b（推荐逻辑贯通卡）：因子分值行保留，新增「为什么」论证行——
从 agent_memory（agent 层已留痕）读论证，经 advisor 忠实性守卫二次加工；
无原料显式「理由生成中」，不回退排名话术充数（诚实水位）。
每条推荐段尾带合规免责句（DISCLAIMER，单一来源 report_layer.advisor）。
契约条款3：只消费 agent 已产出论证（读库），不触发 agent 重算。
"""
from common.logging import get_logger
from report_layer.sections.base import Section, SectionResult
from report_layer.advisor import (DISCLAIMER, PENDING_LINE, load_argument,
                                  persist_advisor, rewrite_to_advisor)
from report_layer.advisor.trace_conn import resolve_trace_conn

logger = get_logger("report.section.stock_picks")


class StockPicksSection(Section):
    name = "stock_picks"

    def _get_agent_memory_conn(self, context):
        """agent_memory 只读连接（data_layer 持库）；取不到返回 None（走诚实水位）"""
        try:
            db = getattr(context.data_layer, "db", None) if context.data_layer else None
            return db.get_connection() if db else None
        except Exception:
            return None

    def _get_rationale_lines(self, context, code: str) -> list[str]:
        """二次加工论证行。原料=agent_memory 当日行；无原料=显式理由生成中。"""
        try:
            conn = self._get_agent_memory_conn(context)
            arg = load_argument(code, context.date, conn)
            if arg is None:
                return [f"    💬 {PENDING_LINE}"]
            result = rewrite_to_advisor(arg, llm=getattr(context, "llm_service", None),
                                        trace_db=resolve_trace_conn(context),
                                        date=context.date)
            lines = [f"    💬 {t}" for t in result.text.split("\n") if t.strip()]
            persist_advisor(conn, result)
            if result.degraded:
                lines.append("    ⚠️ 以上为系统原始分析表述（LLM 改写未通过忠实性校验，已如实展示）")
            return lines
        except Exception as e:
            logger.warning(f"Advisor rationale for {code} failed: {e}")
            return [f"    💬 {PENDING_LINE}"]

    def _get_factor_explanations(self, context, code: str, date: str) -> list[str]:
        explanations = []
        try:
            if context.strategy_layer and hasattr(context.strategy_layer, "get_scores"):
                scores = context.strategy_layer.get_scores(code, date)
                if scores and hasattr(scores, "factors") and scores.factors:
                    factors = scores.factors
                    sorted_factors = sorted(factors.items(), key=lambda x: abs(x[1]), reverse=True)[:4]
                    for fname, fscore in sorted_factors:
                        if abs(fscore) < 0.01:
                            continue
                        cn = self._factor_cn(fname)
                        arrow = "🟢" if fscore > 0 else "🔴"
                        explanations.append(f"    {arrow} {cn}: {fscore:+.2f}")
        except Exception as e:
            logger.warning(f"Factor explanation for {code} failed: {e}")
        return explanations

    def _factor_cn(self, name: str) -> str:
        mapping = {
            "momentum_20d": "动量(20日)",
            "momentum_60d": "动量(60日)",
            "momentum_120d": "动量(120日)",
            "value_discount": "估值折价",
            "ep_ratio": "E/P比率",
            "bp_ratio": "B/P比率",
            "roe_quality": "ROE质量",
            "roa": "ROA",
            "operating_margin": "营业利润率",
            "realized_vol_20d": "波动率(20日)",
            "downside_vol": "下行波动率",
            "amihud_illiquidity": "非流动性",
            "avg_turnover_rate": "换手率",
            "stochastic_k": "随机指标%K",
            "rsi_14": "RSI(14日)",
            "macd_hist": "MACD柱",
            "bb_width": "布林带宽度",
            "moneyflow_net": "资金净流入",
            "market_sentiment": "市场情绪",
            "size_small_cap": "小市值",
            "dividend_yield": "股息率",
            "growth_rate": "增长率",
            "seasonality": "季节性",
            "accrual_ratio": "应计比率",
            "industry_momentum": "行业动量",
            "pattern_score": "形态分数",
            "alpha_48": "Alpha48",
            "earnings_stability": "盈利稳定性",
            "debt_ratio": "负债率",
            "current_ratio": "流动比率",
            "fcf_ev": "FCF/EV",
            "peg_ratio": "PEG",
            "earnings_yield": "E/P收益率",
            "book_yield": "B/P收益率",
            "momentum_strength": "动量强度",
            "momentum_trend": "动量趋势",
            "asset_turnover": "资产周转率",
            "net_profit_margin": "净利润率",
            "eps_factor": "每股收益",
            "bps_factor": "每股净资产",
        }
        return mapping.get(name, name)

    def build(self, context) -> SectionResult | None:
        try:
            if not context.strategy_layer:
                return SectionResult(title="📌 今日精选", body="策略层未接入")
            if hasattr(context.strategy_layer, "run_daily"):
                cache_key = f"run_daily_{context.date}"
                if cache_key not in context._cache:
                    context._cache[cache_key] = context.strategy_layer.run_daily(context.date)
                result = context._cache[cache_key]
                signals = result.signals if hasattr(result, "signals") else []
                regime = result.regime if hasattr(result, "regime") else ""
                if not signals:
                    return SectionResult(title="📌 今日精选", body="暂无推荐")
                lines = [f"📌 今日精选 ({len(signals[:5])} 只)"]
                lines.append("=" * 40)
                if regime:
                    lines.append(f"📊 市场状态: {regime}")
                    lines.append("")
                for s in signals[:5]:
                    code = s.code if hasattr(s, "code") else s.get("code", "")
                    action = s.action if hasattr(s, "action") else s.get("action", "hold")
                    strength = s.strength if hasattr(s, "strength") else s.get("strength", 0)
                    action_icon = "🟢" if action == "buy" else ("🔴" if action == "sell" else "⚪")
                    label = action if action in ("buy", "sell", "hold") else "hold"
                    label_cn = {"buy": "关注", "sell": "回避", "hold": "观望"}[label]
                    lines.append(f"{action_icon} {code}: 建议{label_cn} (强度: {strength:.1%})")
                    # 模块1b: 为什么选它 — 忠实性守卫加工后的论证（无原料=理由生成中）
                    lines.append("    为什么：")
                    lines.extend(self._get_rationale_lines(context, code))
                    explanations = self._get_factor_explanations(context, code, context.date)
                    if explanations:
                        lines.append("    因子参考：")
                        lines.extend(explanations)
                    lines.append(f"    — {DISCLAIMER} —")
                    lines.append("")
                return SectionResult(title="📌 今日精选", body="\n".join(lines),
                                     sources=["agent_memory", "strategy_layer"])
            return SectionResult(title="📌 今日精选", body="暂无推荐")
        except Exception as e:
            logger.warning(f"StockPicksSection build failed: {e}")
            return SectionResult(title="📌 今日精选", body="数据不可用")
