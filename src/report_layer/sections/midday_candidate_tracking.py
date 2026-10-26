"""R3: 🧵 午间候选跟踪 Section — 每候选一行, 一屏读完 (设计稿-C §2.4)。

每只候选一行 = 现价 vs 区间状态 (区间内✅/逼近⚠️/尚远✗) + 上午推送状态
(「HH:MM 已推送」或「未触发」)。数据 = 实时价 + intraday_checks 当日记录。

候选集合与晨报行动卡同口径 (D7 连续性: pending_entry + watch 就绪,
valid_until 未过期, ST 排除) — 保证「晨报推的 = 午报跟的」。

只读: 经 data_layer.db.get_connection() + build_lifelines, 零写入 (铁律#4)。
"""
from __future__ import annotations

from common.logging import get_logger
from report_layer.lifeline import Lifeline, build_lifelines, is_open
from report_layer.sections.base import Section, SectionResult
from report_layer.sections.base import note_degraded

logger = get_logger("report.section.midday_tracking")

# 与晨报行动卡一致的就绪阈值 (watch 且 score > 0.6 才跟踪)
_ENTRY_READY_SCORE = 0.6
# 逼近判定: 现价距区间边界 ≤ 1% 视为逼近
_NEAR_PCT = 0.01


def load_candidates(context) -> list[Lifeline]:
    """候选集合 (与晨报行动卡同口径): pending_entry + watch 就绪, 未过期, 非 ST。

    供跟踪段与 midday_strategy 的 LLM 输入注入共用 — 保证口径单一。
    数据不可用返回空列表 (诚实降级, 不抛)。
    """
    try:
        db = getattr(context.data_layer, "db", None) if context.data_layer else None
        if db is None:
            return []
        conn = db.get_connection()
        lifelines = build_lifelines(conn)
        out = []
        for ll in lifelines:
            if ll.status not in ("pending_entry", "watch"):
                continue
            if "ST" in (ll.name or "").upper():
                continue
            if not is_open(ll, context.date):
                continue
            if ll.status == "watch" and (ll.initial_score or 0) <= _ENTRY_READY_SCORE:
                continue
            out.append(ll)
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning(f"load_candidates failed: {e}")
        return []


def _status_icon(price: float, low: float, high: float) -> str:
    """现价 vs 区间: 区间内✅ / 边界 1% 内⚠️ / 其余✗。"""
    if low <= 0 or high <= 0 or price <= 0:
        return "✗"
    if low <= price <= high:
        return "✅"
    if low * (1 - _NEAR_PCT) <= price < low or high < price <= high * (1 + _NEAR_PCT):
        return "⚠️"
    return "✗"


def _push_status(conn, code: str, as_of: str) -> str:
    """上午推送状态: intraday_checks 当日 entry triggered 首笔时间, 无则未触发。"""
    try:
        row = conn.execute(
            "SELECT check_time FROM intraday_checks "
            "WHERE code=? AND check_type='entry' AND status='triggered' "
            "AND check_time LIKE ? ORDER BY check_time LIMIT 1",
            (code, as_of + "%")).fetchone()
        if row and row[0] and " " in row[0]:
            return f"{row[0].split(' ')[1][:5]} 已推送"
        return "未触发"
    except Exception:  # noqa: BLE001
        return "未触发"


def _block_status(conn, code: str, as_of: str) -> str:
    """D1 续卡: 当日拦截状态 — entry_blocks 最新一条 → 「被拦: 原因」。

    无记录返回 "" (诚实不虚构, 范式 7); 表未建 (GO 前) 亦返回 ""。
    """
    try:
        row = conn.execute(
            "SELECT gate, detail, block_time FROM entry_blocks "
            "WHERE code=? AND block_time LIKE ? "
            "ORDER BY block_time DESC LIMIT 1",
            (code, as_of + "%")).fetchone()
        if not row:
            return ""
        detail = row[1] or row[0] or ""
        at = row[2].split(" ")[1][:5] if (row[2] and " " in row[2]) else ""
        head = f"{at} 被拦" if at else "被拦"
        return f"｜ {head}: {detail}" if detail else ""
    except Exception as _deg_exc:  # noqa: BLE001
        note_degraded("midday_candidate_tracking", _deg_exc)
        return ""


class MiddayCandidateTrackingSection(Section):
    name = "midday_candidate_tracking"

    def build(self, context) -> SectionResult | None:
        try:
            db = getattr(context.data_layer, "db", None) if context.data_layer else None
            if db is None:
                return SectionResult(
                    title="🧵 上午候选跟踪",
                    body="⚪ 数据缺口（行情库不可用），候选跟踪暂不可用",
                    name=self.name)

            candidates = load_candidates(context)
            if not candidates:
                return SectionResult(
                    title="🧵 上午候选跟踪",
                    body="⚪ 今日无候选股在跟踪窗口内，只看不动",
                    name=self.name)

            conn = db.get_connection()
            codes = [ll.code for ll in candidates]
            prices: dict = {}
            try:
                prices = context.data_layer.get_realtime_prices(codes) or {}
            except Exception as e:  # noqa: BLE001
                logger.warning(f"realtime prices failed: {e}")

            lines = [f"🧵 上午候选跟踪 ({len(candidates)} 只)"]
            lines.append("=" * 40)
            for ll in candidates:
                price = float(prices.get(ll.code, 0) or 0)
                low = ll.entry_low or 0
                high = ll.entry_high or 0
                icon = _status_icon(price, low, high)
                price_str = f"{price:.2f}" if price > 0 else "取价失败"
                range_str = f"[{low:.2f}, {high:.2f}]" if low > 0 and high > 0 else "区间缺失"
                push = _push_status(conn, ll.code, context.date)
                # D1 续卡: 当日被闸门拦截 → 行尾追加被拦原因 (诚实可见)
                block = _block_status(conn, ll.code, context.date)
                lines.append(
                    f"{icon} {ll.name}({ll.code}) 现价 {price_str} vs {range_str} ｜ {push}{block}")
            return SectionResult(title="🧵 上午候选跟踪",
                                 body="\n".join(lines),
                                 name=self.name,
                                 sources=["lifeline", "realtime", "intraday_checks"])
        except Exception as e:  # noqa: BLE001
            logger.warning(f"MiddayCandidateTracking build failed: {e}")
            return SectionResult(
                title="🧵 上午候选跟踪",
                body="⚪ 数据缺口（读取异常），候选跟踪暂不可用",
                name=self.name)
