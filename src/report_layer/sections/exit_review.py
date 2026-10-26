"""退出回顾 Section — 含 PostExitTracker 数据 (F5.3: LLM 退出复盘归因 + 规则降级)"""
from report_layer.sections.base import Section, SectionResult
from report_layer.sections._llm_helper import llm_call
from report_layer.sections.base import note_degraded


class ExitReviewSection(Section):
    name = "exit_review"

    def _get_exited_positions(self, context) -> list | None:
        if not context.execution_layer:
            return None
        try:
            stock_pool = context.execution_layer.stock_pool
            if not stock_pool:
                return None
            exited = stock_pool.get_by_status("exited")
            return exited or []
        except Exception as _deg_exc:
            note_degraded("exit_review", _deg_exc)
            return None

    def _get_post_exit_tracker(self, context) -> object | None:
        if not context.execution_layer:
            return None
        try:
            return getattr(context.execution_layer, "post_exit_tracker", None)
        except Exception as _deg_exc:
            note_degraded("exit_review", _deg_exc)
            return None

    def build(self, context) -> SectionResult | None:
        try:
            exited = self._get_exited_positions(context)
            if exited is None:
                return SectionResult(title="退出回顾", body="执行层不可用")
            if not exited:
                return SectionResult(title="退出回顾", body="✅ 无已退出记录")
            recent = sorted(exited, key=lambda x: x.get("exit_date", ""), reverse=True)[:10]
            lines = [f"📋 退出回顾 ({len(recent)} 条)"]
            lines.append("=" * 40)
            for item in recent:
                code = item.get("code", "")
                name = item.get("name", "") or ""
                entry_date = item.get("entry_date", "")
                exit_date = item.get("exit_date", "")
                # F5.5: 收益率优先 net_return_pct (含费净收益), 回退 total_return_pct
                ret = item.get("net_return_pct") or item.get("total_return_pct", 0) or 0
                reason = item.get("exit_reason", "") or ""
                quality = item.get("review_quality", "") or ""
                # F5.5: stock_pool 真实列名 holding_days_actual (非 days_held)
                days_held = item.get("holding_days_actual", 0) or 0
                emoji = "🟢" if ret > 0 else "🔴"
                lines.append(f"  {emoji} {code} {name}")
                lines.append(f"    收益: {ret:+.2f}% | 持有: {days_held}天 | 质量: {quality}")
                lines.append(f"    入场: {entry_date} → 出场: {exit_date}")
                if reason:
                    lines.append(f"    原因: {reason}")
            tracker = self._get_post_exit_tracker(context)
            if tracker:
                # F5.5: PostExitTracker 无 get_recent; 按已展示股票取 get_tracking,
                # 真实列名 check_point / price_change_since_exit / quality
                post_lines = []
                for item in recent[:5]:
                    try:
                        tracks = tracker.get_tracking(item.get("code", ""))
                    except Exception:
                        continue
                    judged = [t for t in tracks if t.get("price_change_since_exit") is not None]
                    if not judged:
                        continue
                    parts = ", ".join(
                        f"{t.get('check_point', '')} {t.get('price_change_since_exit', 0):+.1%}"
                        for t in judged[:3])
                    post_lines.append(f"  {item.get('code', '')}: {parts}")
                if post_lines:
                    lines.append("")
                    lines.append("📊 退出后表现:")
                    lines.extend(post_lines)

            # F5.3: LLM 退出复盘归因 (失败 → 仅规则列表, 不阻塞)
            recap_items = []
            for item in recent[:5]:
                ret = item.get("net_return_pct") or item.get("total_return_pct", 0) or 0
                recap_items.append(
                    f"{item.get('name', '') or item.get('code', '')}"
                    f"(收益{ret:+.1f}%, 原因:{item.get('exit_reason', '') or '未知'})")
            if recap_items:
                prompt = (
                    f"近期退出的持仓: {'; '.join(recap_items)}。\n"
                    f"请用2-3句话复盘这批退出的共性原因与可改进之处，不要markdown。"
                )
                recap = llm_call(context, prompt, call_point="exit_review",
                                 temperature=0.4, max_tokens=2000)
                if recap:
                    lines.append("")
                    lines.append(f"🧠 复盘: {recap}")
                    return SectionResult(title="退出回顾", body="\n".join(lines),
                                         sources=["rule-based", "LLM"])
            return SectionResult(title="退出回顾", body="\n".join(lines),
                                 sources=["rule-based"])
        except Exception:
            return SectionResult(title="退出回顾", body="数据不可用")
