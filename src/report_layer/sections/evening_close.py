"""R3: 🌙 三段式收尾 Section — 财信式 (设计稿-C §2.5 第 6 段)。

今日复盘 + 总体研判 + 明日方向。
明日方向必须列候选股延续计划 = 次日连续性钩子 (闭环到次日晨报第 3 段
「昨日候选, 现在如何」), 杜绝「今天没推荐但明天买了」。

只读: 经 data_layer.db.get_connection() + build_lifelines, 零写入 (铁律#4)。
"""
from __future__ import annotations

from common.logging import get_logger
from report_layer.lifeline import build_lifelines, is_open
from report_layer.sections.base import Section, SectionResult

logger = get_logger("report.section.evening_close")


class EveningCloseSection(Section):
    name = "evening_close"

    def build(self, context) -> SectionResult | None:
        try:
            db = getattr(context.data_layer, "db", None) if context.data_layer else None
            if db is None:
                return SectionResult(
                    title="🌙 收尾",
                    body="⚪ 数据缺口（行情库不可用），收尾暂不可用",
                    name=self.name)
            conn = db.get_connection()
            lifelines = build_lifelines(conn)

            bought = [ll for ll in lifelines
                      if ll.entry_date or ll.status in ("active", "exit_pending", "exited")]
            open_lifelines = [ll for ll in lifelines
                              if is_open(ll, context.date)
                              and "ST" not in (ll.name or "").upper()
                              and ll.status in ("pending_entry", "watch")]

            lines = ["【今日复盘】"]
            lines.append(
                f"今日生命线 {len(lifelines)} 条：已买入 {len(bought)} 只，"
                f"在跟踪窗口内 {len(open_lifelines)} 只。")

            lines.append("")
            lines.append("【总体研判】")
            if bought:
                lines.append("今日有候选触价买入，策略按计划执行。")
            elif open_lifelines:
                lines.append("今日候选未触价，保持观察，不追高。")
            else:
                lines.append("今日无候选在跟踪窗口内，只看不动。")

            lines.append("")
            lines.append("【明日方向】")
            if open_lifelines:
                names = "、".join(f"{ll.name}({ll.code})" for ll in open_lifelines[:5])
                lines.append(f"候选延续计划：{names} 继续在买入区间跟踪，"
                             f"触价即推送（闭环到明早晨报）。")
            else:
                lines.append("暂无候选延续计划，等待新信号入池。")
            return SectionResult(title="🌙 收尾",
                                 body="\n".join(lines),
                                 name=self.name,
                                 sources=["lifeline"])
        except Exception as e:  # noqa: BLE001
            logger.warning(f"EveningClose build failed: {e}")
            return SectionResult(
                title="🌙 收尾",
                body="⚪ 数据缺口（读取异常），收尾暂不可用",
                name=self.name)
