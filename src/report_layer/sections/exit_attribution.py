# -*- coding: utf-8 -*-
"""📋 退出归因卡 Section — 只列有实质复盘者 (TASK_20260922_01 Phase B)。

每笔退出 = 推荐理由 → 持有天数 → 退出层级 → 盈亏 → 一句教训,
但空字段行直接隐藏 (批复放行条件: 「未知层级」「持有 未知」
「盈亏未记录」「watch_expired」等空/英文占位不再渲染):
  - exit_layer 空 → 不显示退出层级位;
  - review_note 空 → 不显示教训行;
  - exit_reason 英文内部码 → 展示层中文化 (数据库值不变);
  - 无任何复盘内容 (无盈亏记录且无教训) 的卡不上榜, 计数折叠声明
    「另有 N 只退出记录暂无复盘，完整故事线见深钻页 P0」。

推荐理由过单点闸 report_reason._pick_reason (排名模板永不进卡, 裁决3)。
只读: 经 data_layer.db.get_connection() + build_lifelines, 零写入 (铁律#4)。
"""
from __future__ import annotations

import re

from common.logging import get_logger
from report_layer.lifeline import build_lifelines
from report_layer.sections.base import Section, SectionResult
from report_reason import _pick_reason_card

logger = get_logger("report.section.exit_attribution")

_MAX_CARDS = 3  # 少即是多: 有实质复盘的退出也只上榜 3 张, 全量见深钻页 P0

# 英文内部码 → 中文 (展示层映射, 数据库值不变)
_EXIT_REASON_CN = {
    "watch_expired": "观察期到期未触发，计划退出",
    "q54_stale_pending_entry": "待入场超时未触发，计划退出",
}

_CJK_RE = re.compile(r"[一-鿿]")


def _cn_exit_reason(raw: str) -> str:
    """exit_reason 中文化: 含中文原样; 纯英文内部码查映射, 未收录则深藏不露
    (内部码原样渲染对用户是黑话, 隐藏比直出诚实)。"""
    raw = (raw or "").strip()
    if not raw:
        return ""
    if _CJK_RE.search(raw):
        return raw
    return _EXIT_REASON_CN.get(raw, "")


def _has_substance(ll) -> bool:
    """有实质复盘 = 有盈亏记录 或 有教训备注 (空字段卡不上榜)。"""
    ret = ll.total_return_pct if ll.total_return_pct is not None else ll.net_return_pct
    return ret is not None or bool((ll.review_note or "").strip())


class ExitAttributionSection(Section):
    name = "exit_attribution"

    def build(self, context) -> SectionResult | None:
        try:
            db = getattr(context.data_layer, "db", None) if context.data_layer else None
            if db is None:
                return SectionResult(
                    title="📋 退出归因",
                    body="⚪ 数据缺口（行情库不可用），退出归因暂不可用",
                    name=self.name)
            conn = db.get_connection()
            lifelines = build_lifelines(conn)
            exited = [ll for ll in lifelines if ll.status == "exited"]
            if not exited:
                return SectionResult(
                    title="📋 退出归因",
                    body="暂无退出记录",
                    name=self.name)

            substantive = [ll for ll in exited if _has_substance(ll)]
            hidden = len(exited) - len(substantive)
            if not substantive:
                body = (f"暂无有复盘内容的退出记录"
                        f"（{len(exited)} 只退出明细见深钻页 P0 完整故事线）")
                return SectionResult(title="📋 退出归因", body=body,
                                     name=self.name)

            # 最近退出在前 (exit_date 降序, 空值垫底)
            substantive.sort(key=lambda ll: ll.exit_date or "", reverse=True)
            lines = []
            for ll in substantive[:_MAX_CARDS]:
                # P0-1 (2026-09-28): 论证卡 — rationale 通电后自动升级多字段卡
                # 判例#132: 传 code + conn/date 才能读 rationale 论证卡 (Day3 补锚)
                reason = _pick_reason_card({
                    "code": ll.code,
                    "added_reason": ll.added_reason or ll.entry_reason},
                    conn=conn, date=context.date if hasattr(context, "date") else None)
                ret = (ll.total_return_pct if ll.total_return_pct is not None
                       else ll.net_return_pct)
                facts = []
                if ll.holding_days_actual is not None:
                    facts.append(f"持有 {ll.holding_days_actual} 天")
                if ll.exit_layer:
                    facts.append(f"退出层级：{ll.exit_layer}")
                if ret is not None:
                    facts.append(f"盈亏 {ret:+.1f}%")
                lines.append(f"📋 {ll.name}({ll.code})")
                lines.append(f"  推荐理由：{reason}")
                if facts:
                    lines.append("  " + " ｜ ".join(facts))
                exit_why = _cn_exit_reason(ll.exit_reason) or \
                    _cn_exit_reason(ll.exit_reason_detail)
                if exit_why:
                    lines.append(f"  退出原因：{exit_why}")
                if (ll.review_note or "").strip():
                    lines.append(f"  教训：{ll.review_note.strip()}")
            if hidden:
                lines.append(
                    f"另有 {hidden} 只退出记录暂无复盘"
                    f"（无盈亏/教训），完整故事线见深钻页 P0")
            return SectionResult(title="📋 退出归因",
                                 body="\n".join(lines).rstrip(),
                                 name=self.name,
                                 sources=["lifeline", "stock_pool"])
        except Exception as e:  # noqa: BLE001
            logger.warning(f"ExitAttribution build failed: {e}")
            return SectionResult(
                title="📋 退出归因",
                body="⚪ 数据缺口（读取异常），退出归因暂不可用",
                name=self.name)
