# -*- coding: utf-8 -*-
"""昨日推荐点评 (报告宪法 #2: 晚报置顶信誉位)。

昨日 stock_pool 推荐票的今日表现, 三态逐票一行:
    已触发买入 (entry_date 落库) → 浮盈% (EX-3 快照/last_price 口径)
    未触发 (watch/计划内)        → 等/废 (valid_until 口径)
    过期作废 (valid_until 已过)  → 废, 诚实水位绝不静默消失
每票尾附后续操作建议 (距止损线/持有观察/作废清理)。
浮盈数据缺 (unrealized_pnl_pct NULL 且无快照) → 该票行诚实「快照积累中」。
只读零写入 (铁律 #4)。
"""
from __future__ import annotations

from datetime import date as _date, timedelta

from common.logging import get_logger
from report_layer.sections.base import Section, SectionResult
from report_layer.sections.base import note_degraded

logger = get_logger("report.section.yesterday_picks_review")


def _prev_day(as_of: str) -> str:
    try:
        return (_date.fromisoformat(as_of) - timedelta(days=1)).isoformat()
    except ValueError as _deg_exc:
        note_degraded("yesterday_picks_review", _deg_exc)
        return ""


class YesterdayPicksReviewSection(Section):
    name = "yesterday_picks_review"

    def build(self, context) -> SectionResult | None:
        try:
            db = getattr(context.data_layer, "db", None) if context.data_layer else None
            if db is None:
                return None
            conn = db.get_connection()
            yesterday = _prev_day(context.date)
            if not yesterday:
                return None

            rows = conn.execute(
                "SELECT code, name, status, entry_date, entry_price, "
                "unrealized_pnl_pct, last_price, valid_until, stop_loss, "
                "added_date FROM stock_pool "
                "WHERE added_date = ? OR entry_date = ? OR valid_until = ?",
                (yesterday, yesterday, yesterday),
            ).fetchall()

            lines = []
            for r in rows:
                (code, name, status, entry_date, entry_price, upnl,
                 last_price, valid_until, stop, added) = r
                label = f"{name}({code})" if name else code
                if entry_date:
                    # 已触发买入 → 浮盈% (快照优先, 缺则诚实)
                    if isinstance(upnl, (int, float)):
                        action = self._holding_advice(stop, last_price)
                        lines.append(
                            f"✅ {label}：昨日触发买入，现 {upnl:+.1f}%{action}")
                    else:
                        lines.append(
                            f"✅ {label}：昨日触发买入，现价快照积累中")
                elif status == "exited" or (
                        valid_until and valid_until < context.date):
                    lines.append(f"⚪ {label}：昨日推荐到期未触发，作废")
                else:
                    left = ""
                    if valid_until:
                        try:
                            d = (_date.fromisoformat(valid_until)
                                 - _date.fromisoformat(context.date)).days
                            left = f"，余 {d} 天" if d >= 0 else ""
                        except ValueError:
                            left = ""
                    lines.append(f"⏳ {label}：未触发，继续等待{left}，建议观察量能配合")

            if not lines:
                return SectionResult(
                    title="🔁 昨日推荐点评", body="昨日无推荐（快照积累中）",
                    name=self.name)
            body = "\n".join(
                [f"🔁 昨日推荐点评 ({len(lines)} 只)", "=" * 40, *lines])
            return SectionResult(title="🔁 昨日推荐点评", body=body,
                                 name=self.name, sources=["stock_pool"])
        except Exception as e:
            logger.warning(f"YesterdayPicksReviewSection build failed: {e}")
            return None

    @staticmethod
    def _holding_advice(stop, last) -> str:
        try:
            if stop and last and last > 0:
                dist = (last - stop) / last * 100
                return f"，距参考卖出线 {dist:.1f}%，建议按计划持有"
        except (TypeError, ZeroDivisionError) as _deg_exc:
            note_degraded("yesterday_picks_review", _deg_exc)
            pass
        return "，建议按计划持有"
