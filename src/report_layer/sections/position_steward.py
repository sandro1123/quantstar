# -*- coding: utf-8 -*-
"""持仓管家 (宪法用户愿景): 持仓+跟踪池一表 (现价/浮盈/距参考卖出线%/建议)。

状态展示中文化 (仅展示层映射, 数据库值不变): active→持有中 / watch→观察中 /
pending_entry→待买入 / exit_pending→待卖出。
"""
from __future__ import annotations

from common.logging import get_logger
from report_layer.sections.base import Section, SectionResult

logger = get_logger("report.section.position_steward")

# 展示层状态中文化映射 (仅卡片/报告展示, 数据库值不变)
STATUS_CN = {"active": "持有中", "watch": "观察中",
             "pending_entry": "待买入", "exit_pending": "待卖出"}


class PositionStewardSection(Section):
    name = "position_steward"

    def build(self, context) -> SectionResult | None:
        try:
            el = getattr(context, "execution_layer", None)
            if el is None or not hasattr(el, "get_portfolio_snapshot"):
                db = getattr(context.data_layer, "db", None) if context.data_layer else None
                if db is None:
                    return None
                conn = db.get_connection()
                rows = conn.execute(
                    "SELECT code, name, status, last_price, unrealized_pnl_pct, "
                    "stop_loss FROM stock_pool WHERE status IN "
                    "('active', 'watch', 'pending_entry', 'exit_pending') "
                    "ORDER BY status, code").fetchall()
                if not rows:
                    return SectionResult(title="🏠 持仓管家",
                                         body="持仓与跟踪池为空", name=self.name)
                lines = ["🏠 持仓管家（持仓+跟踪池）", "=" * 40,
                         "代码 | 状态 | 现价 | 浮盈% | 距参考卖出线% | 建议"]
                for (code, name, status, last, upnl, stop) in rows:
                    pnl_s = f"{upnl:+.1f}%" if isinstance(upnl, (int, float)) else "快照积累中"
                    if stop and last and last > 0:
                        dist = f"{(last - stop) / last * 100:.1f}%"
                        advice = "按计划持有" if status == "active" else "观察量能"
                    else:
                        dist = "—"
                        advice = "等待快照"
                    lines.append(f"{name or code} | {STATUS_CN.get(status, status)} | "
                                 f"{last if last else '—'} | {pnl_s} | {dist} | {advice}")
                return SectionResult(title="🏠 持仓管家",
                                     body="\n".join(lines), name=self.name,
                                     sources=["stock_pool"])
            snap = el.get_portfolio_snapshot()
            lines = [f"🏠 持仓管家（持有中 {snap.active} / 观察中 {snap.watch}）",
                     "=" * 40]
            for pos in getattr(snap, "positions", []):
                def _g(k, d=None):
                    if hasattr(pos, "__getitem__"):
                        return pos.get(k, d)
                    return getattr(pos, k, d)
                code = _g("code", "")
                name = _g("name", "") or code
                pnl = _g("pnl_pct", 0) or 0
                stop = _g("stop_loss", 0) or 0
                last = _g("last_price", 0) or 0
                dist = f"{(last - stop) / last * 100:.1f}%" if (stop and last) else "—"
                lines.append(f"{name}({code}) 现 {pnl:+.1f}%，距参考卖出线 {dist}")
            return SectionResult(title="🏠 持仓管家",
                                 body="\n".join(lines) if len(lines) > 2 else "持仓为空",
                                 name=self.name, sources=["stock_pool"])
        except Exception as e:
            logger.warning(f"PositionStewardSection build failed: {e}")
            return SectionResult(title="🏠 持仓管家", body="持仓数据积累中",
                                  name=self.name)
