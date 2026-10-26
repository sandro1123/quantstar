"""板块分析 Section — 含行业数据查询 (F5.3: LLM 板块研判 + 规则降级)"""
from report_layer.sections.base import Section, SectionResult
from report_layer.sections._llm_helper import llm_call
from report_layer.sections.base import note_degraded


class SectorAnalysisSection(Section):
    name = "sector_analysis"

    def _get_sector_data(self, context) -> SectionResult | None:
        if not context.data_layer:
            return None
        try:
            snapshot = context.data_layer.snapshot(context.date)
            if not snapshot or not snapshot.klines:
                return None
            sectors = {}
            for k in snapshot.klines:
                sector = getattr(k, "sector", None) or getattr(k, "industry", "未知")
                change = getattr(k, "change_pct", 0) or 0
                vol_ratio = getattr(k, "volume_ratio", 1.0) or 1.0
                amount = getattr(k, "amount", 0) or 0
                if sector not in sectors:
                    sectors[sector] = {"count": 0, "up": 0, "down": 0, "change_sum": 0.0, "vol_sum": 0.0, "amount_sum": 0.0}
                sectors[sector]["count"] += 1
                sectors[sector]["change_sum"] += change
                sectors[sector]["vol_sum"] += vol_ratio
                sectors[sector]["amount_sum"] += amount
                if change > 0:
                    sectors[sector]["up"] += 1
                elif change < 0:
                    sectors[sector]["down"] += 1
            return sectors
        except Exception as _deg_exc:
            note_degraded("sector_analysis", _deg_exc)
            return None

    def build(self, context) -> SectionResult | None:
        try:
            sectors = self._get_sector_data(context)
            if not sectors:
                return SectionResult(title="板块分析", body="无板块数据")
            sorted_sectors = sorted(sectors.items(), key=lambda x: x[1]["count"], reverse=True)
            lines = [f"📊 板块分析 ({context.date})"]
            lines.append("=" * 40)
            for sector, data in sorted_sectors[:10]:
                avg_change = data["change_sum"] / data["count"] if data["count"] else 0
                avg_vol = data["vol_sum"] / data["count"] if data["count"] else 0
                arrow = "🟢" if avg_change > 0.5 else ("🔴" if avg_change < -0.5 else "⚪")
                lines.append(f"{arrow} {sector}: {data['count']}只 | "
                             f"涨{data['up']}跌{data['down']} | "
                             f"均涨跌{avg_change:+.2f}% | "
                             f"量比{avg_vol:.1f}x")
            top = sorted(sectors.items(), key=lambda x: x[1]["change_sum"] / x[1]["count"], reverse=True)[:3]
            bottom = sorted(sectors.items(), key=lambda x: x[1]["change_sum"] / x[1]["count"])[:3]
            lines.append("")
            lines.append("🏆 强势板块:")
            for s, d in top:
                avg = d["change_sum"] / d["count"]
                lines.append(f"  {s}: {avg:+.2f}%")
            lines.append("📉 弱势板块:")
            for s, d in bottom:
                avg = d["change_sum"] / d["count"]
                lines.append(f"  {s}: {avg:+.2f}%")

            # F5.3: LLM 板块研判 (失败 → 仅规则统计, 不阻塞)
            top_txt = "、".join(f"{s}({d['change_sum']/d['count']:+.2f}%)" for s, d in top)
            bottom_txt = "、".join(f"{s}({d['change_sum']/d['count']:+.2f}%)" for s, d in bottom)
            prompt = (
                f"今日A股强势板块: {top_txt}; 弱势板块: {bottom_txt}。\n"
                f"请用2-3句话点评今日板块轮动特征与明日关注方向，不要markdown。"
            )
            # AG-WIRE-3 首试点开闸 (统筹 2026-09-08): sector 研判走辩论链
            # (六专家+裁决官), 叙述质量升级 + agent_predictions 留痕; 失败/超时
            # 自动回落原 llm_call 轨 (叙述柜台), 节渲染零回退面。后续逐节开闸
            # 参考此实参形态 (task 前缀 "multi_agent_review:<节>" 进 token_bucket
            # 3000 独立预算桶)。
            comment = llm_call(context, prompt, call_point="sector_analysis",
                               temperature=0.5, max_tokens=2000,
                               task="multi_agent_review:sector",
                               style="researcher",
                               max_words=200,
                               focus=["sector_momentum", "capital_flow"])
            if comment:
                lines.append("")
                lines.append(f"🧠 研判: {comment}")
                return SectionResult(title="板块分析", body="\n".join(lines),
                                     sources=["rule-based", "LLM"])
            return SectionResult(title="板块分析", body="\n".join(lines),
                                 sources=["rule-based"])
        except Exception:
            return SectionResult(title="板块分析", body="数据不可用")
