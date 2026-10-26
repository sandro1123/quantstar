"""投资结论 Section — 值得买/观望/不建议 (F5.3: LLM 综合研判 + 规则降级)"""
from report_layer.sections.base import Section, SectionResult
from report_layer.sections._llm_helper import llm_call
from common.logging import get_logger
from report_layer.sections.base import note_degraded

logger = get_logger("report.section.conclusion")


class ConclusionSection(Section):
    name = "conclusion"

    def build(self, context) -> SectionResult | None:
        try:
            if not context.data_layer:
                return None

            conn = context.data_layer.db.get_connection()

            # 检查 daily_klines 表是否存在
            if not conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='daily_klines'").fetchone():
                return SectionResult(title="📋 今日操作建议", body="行情数据暂不可用，建议观望", name="conclusion")

            # 获取市场概况
            row = conn.execute("""
                SELECT AVG(change_pct), COUNT(*),
                       SUM(CASE WHEN change_pct>0 THEN 1 ELSE 0 END),
                       SUM(CASE WHEN change_pct>=9.8 THEN 1 ELSE 0 END),
                       SUM(CASE WHEN change_pct<=-9.8 THEN 1 ELSE 0 END)
                FROM daily_klines WHERE date=?
            """, (context.date,)).fetchone()

            if not row or not row[1]:
                return SectionResult(title="📋 今日操作建议", body="市场数据暂不可用，建议观望", name="conclusion")

            avg, total, up, zt, dt = row
            up_r = (up or 0) / max(total, 1) * 100

            # 持仓/信号摘要 (LLM 输入)
            pos_summary = ""
            if context.execution_layer:
                try:
                    snap = context.execution_layer.get_portfolio_snapshot()
                    pos_summary = (f"持仓: 持有中 {getattr(snap, 'active', 0)} 只, "
                                   f"待买入 {getattr(snap, 'pending_entry', 0)} 只, "
                                   f"今日盈亏 {getattr(snap, 'today_pnl', 0):+.2f}")
                except Exception as _deg_exc:
                    note_degraded("conclusion", _deg_exc)
                    pass

            # F5.3: LLM 综合研判; 失败 → 规则降级
            prompt = (
                f"今日A股市场: 平均涨跌 {avg:+.2f}%, 共 {total} 只, "
                f"上涨 {up} 只 ({up_r:.0f}%), 涨停 {zt or 0} 只, 跌停 {dt or 0} 只。"
                + (f"\n组合: {pos_summary}" if pos_summary else "") + "\n\n"
                "请给出今日操作结论，不要markdown，严格按以下格式:\n"
                "【结论】值得买/可以考虑/观望/不建议买 之一\n"
                "【依据】一句话\n【操作】仓位与操作建议\n【风险】一句话风险提示"
            )
            llm_body = llm_call(context, prompt, call_point="conclusion",
                                temperature=0.3, max_tokens=2000)
            if llm_body:
                body = (
                    f"{llm_body}\n\n"
                    f"<font color='grey'>以上建议基于量化模型+AI研判，仅供参考</font>"
                )
                return SectionResult(title="📋 今日操作建议", body=body, name="conclusion",
                                     sources=["LLM"])

            # 规则降级 (原阈值逻辑)
            if up_r > 65 and (zt or 0) > 30:
                verdict = "🟢 值得买"
                reason = f"市场强势（上涨{up_r:.0f}%，涨停{zt}只），短线情绪活跃"
                action = "可适当加仓，关注强势板块龙头"
                risk = "注意追高风险，控制仓位在 6-7 成"
            elif up_r > 55:
                verdict = "🟡 可以考虑"
                reason = f"市场偏强（上涨{up_r:.0f}%，涨停{zt}只）"
                action = "精选个股，轻仓参与"
                risk = "设好止损，仓位 5 成"
            elif up_r > 45:
                verdict = "⚪ 观望"
                reason = f"市场震荡（上涨{up_r:.0f}%，涨停{zt}只/跌停{dt}只）"
                action = "持仓不动，等待方向明确"
                risk = "控制仓位在 4 成以下"
            else:
                verdict = "🔴 不建议买"
                reason = f"市场承压（上涨仅{up_r:.0f}%，跌停{dt}只）"
                action = "减仓防御，回避高位股"
                risk = "仓位控制在 3 成以下，关注防御板块"

            body = (
                f"**结论：{verdict}**\n\n"
                f"📊 依据：{reason}\n"
                f"💡 操作：{action}\n"
                f"⚠️ 风险：{risk}\n\n"
                f"<font color='grey'>以上建议基于量化模型，仅供参考</font>"
            )
            return SectionResult(title="📋 今日操作建议", body=body, name="conclusion",
                                 sources=["rule-based"])
        except Exception as e:
            logger.warning(f"ConclusionSection build failed: {e}")
            return None
