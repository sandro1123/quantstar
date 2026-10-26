"""因子IC权重 Section — 哪些因子最近有效"""
from common.logging import get_logger
from report_layer.sections.base import Section, SectionResult
from report_layer.sections.base import note_degraded

logger = get_logger("report.section.ic_weight")


class ICWeightSection(Section):
    name = "ic_weight"

    def _factor_cn(self, name: str) -> str:
        mapping = {
            "momentum_20d": "动量(20日)", "momentum_60d": "动量(60日)",
            "value_discount": "估值折价", "ep_ratio": "E/P比率",
            "bp_ratio": "B/P比率", "roe_quality": "ROE质量",
            "roa": "ROA", "operating_margin": "营业利润率",
            "realized_vol_20d": "波动率(20日)", "downside_vol": "下行波动率",
            "amihud_illiquidity": "非流动性", "avg_turnover_rate": "换手率",
            "rsi_14": "RSI(14日)", "macd_hist": "MACD柱",
            "moneyflow_net": "资金净流入", "market_sentiment": "市场情绪",
            "dividend_yield": "股息率", "earnings_stability": "盈利稳定性",
            "momentum_strength": "动量强度", "momentum_trend": "动量趋势",
            "fcf_ev": "FCF/EV", "peg_ratio": "PEG",
            "asset_turnover": "资产周转率", "net_profit_margin": "净利润率",
        }
        return mapping.get(name, name)

    def build(self, context) -> SectionResult | None:
        try:
            if not context.strategy_layer:
                return SectionResult(title="📊 因子IC权重", body="策略层未接入")
            factor_ic_data = {}
            # strategy_layer 当前无 .feedback 属性, 尝试 ic_feedback_tuner 字段
            tuner = getattr(context.strategy_layer, "ic_feedback_tuner", None) or \
                    getattr(context.strategy_layer, "_ic_tuner", None)
            if tuner and hasattr(tuner, "get_factor_ic"):
                try:
                    # 无参形态 = 全因子 {name: mean_ic} (F席 09-16 契约修:
                    # 旧无参必 TypeError 被吞成静默降级)。返回 FactorIC/None 的
                    # 旧实现不可作为本段数据源 → 只在拿到 dict 时采信。
                    _got = tuner.get_factor_ic()
                    factor_ic_data = _got if isinstance(_got, dict) else {}
                except Exception as _deg_exc:
                    note_degraded("ic_weight", _deg_exc)
                    pass
            # F5.5: 备用表是 factor_ic (非 factor_evaluations), 列 calc_date/factor_name/mean_ic
            if not factor_ic_data and context.data_layer:
                try:
                    db = getattr(context.data_layer, "db", None)
                    if db:
                        conn = db.get_connection()
                        rows = conn.execute(
                            "SELECT factor_name, mean_ic FROM factor_ic "
                            "WHERE calc_date = (SELECT MAX(calc_date) FROM factor_ic)"
                        ).fetchall()
                        factor_ic_data = {r[0]: r[1] for r in rows if r[1] is not None}
                except Exception as _deg_exc:
                    note_degraded("ic_weight", _deg_exc)
                    pass
            if factor_ic_data:
                sorted_factors = sorted(factor_ic_data.items(), key=lambda x: abs(x[1]), reverse=True)[:8]
                lines = [f"📊 因子IC权重 ({context.date})"]
                lines.append("=" * 40)
                for fname, ic in sorted_factors:
                    if abs(ic) < 0.001:
                        continue
                    cn = self._factor_cn(fname)
                    arrow = "🟢" if ic > 0 else "🔴"
                    lines.append(f"{arrow} {cn}: IC={ic:+.3f}")
                return SectionResult(title="📊 因子IC权重", body="\n".join(lines))
            return SectionResult(title="📊 因子IC权重", body="IC数据暂不可用")
        except Exception as e:
            logger.warning(f"ICWeightSection build failed: {e}")
            return SectionResult(title="📊 因子IC权重", body="数据不可用")
