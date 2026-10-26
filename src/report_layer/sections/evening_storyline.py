# -*- coding: utf-8 -*-
"""🧵 今日候选故事线 — 按动作分组聚合摘要 (TASK_20260922_01 Phase B · 批复裁决2)。

少即是多: 146 只逐只罗列 → 四组聚合 —
  持有中 · 持续监控 : 三行式 _holdings_monitor (覆盖行/待行动行/一致行,
                     批复裁决2「持续监控、全周期建议的机制」, 用户亲定);
  已退出          : 计数 + 胜率 + 盈亏 Top3/Bottom3 各一行;
  未触价·计划继续  : 一行计数 + 仅列有效余期 ≤2 天者 (用户需要行动决策的);
  已过期·计划作废  : 一行计数 (诚实水位: 绝不静默消失)。

铁原则2: 进了明日推荐 Top3 的票不再进未触价列表 (tomorrow_picks.pick_codes);
完整全量落 marimo P0 (5 分钟层), 卡片不再装下 146 只。

只读: 经 data_layer.db.get_connection() + build_lifelines, 零写入 (铁律#4)。
"""
from __future__ import annotations

from datetime import date as _date

from common.logging import get_logger
from report_layer.lifeline import Lifeline, build_lifelines, is_open
from report_layer.sections.base import Section, SectionResult
from report_layer.sections.tomorrow_picks import pick_codes

logger = get_logger("report.section.evening_storyline")

_HELD_STATUSES = ("active", "exit_pending")

# 持有监控阈值 (待行动行): 距参考卖出线 ≤3% / 距目标位 ≤3% / 触价临近 ≤2%
_STOP_NEAR_PCT = 3.0
_TARGET_NEAR_PCT = 3.0
_TOUCH_NEAR_PCT = 2.0

# 未触价组仅列有效余期 ≤2 天者 (行动决策窗口)
_DUE_SOON_DAYS = 2


def _days_left(valid_until: str, as_of: str) -> int:
    try:
        return (_date.fromisoformat(str(valid_until)[:10])
                - _date.fromisoformat(as_of)).days
    except ValueError:
        return 0


def _tracking_tables_present(conn) -> bool:
    """推荐跟踪两表 (recommendation_tracking + advice, W1 1a/1b) 是否已落地。"""
    try:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
    except Exception:  # noqa: BLE001
        return False
    return "recommendation_tracking" in names and "advice" in names


def _holdings_monitor(conn, lifelines: list[Lifeline]) -> list[str]:
    """持有三行式 (批复裁决2): 覆盖行 / 待行动行 / 一致行 — 永不逐只罗列。

    数据源适配器 (与 _pick_reason 同哲学: 接口先对, 数据到了零改动升级):
      - recommendation_tracking + advice 表存在 → 读之 (批复原义, 覆盖行
        计数仍以生命线归并为锚, 表仅作落地存证);
      - 不存在 → 回退 stock_pool 派生并在覆盖行诚实标注。
      - 理由失效/翻转 (is_reversal) 回退路径无数据可派生 → 不列 (诚实水位:
        没有就没有, 每条待行动必须附当期依据)。
    """
    tracked = [ll for ll in lifelines if ll.status != "exited"]
    held = [ll for ll in tracked if ll.status in _HELD_STATUSES]
    watch = [ll for ll in tracked if ll.status == "watch"]
    pending = [ll for ll in tracked if ll.status == "pending_entry"]

    if _tracking_tables_present(conn):
        src_note = ""
    else:
        # 回退路径诚实标注 (批复授权形态, 表上线后自动切换)
        src_note = "（按持仓快照统计：推荐跟踪明细表未上线，上线后自动切换）"
    lines = [
        f"{len(tracked)} 只推荐股全部在跟踪中"
        f"（持有中 {len(held)} ｜ 观察中 {len(watch)} ｜ 待买入 {len(pending)}）"
        f"{src_note}"]

    actions: list[str] = []
    acted_codes: set[str] = set()
    for ll in held:
        last, stop = ll.last_price, (ll.dynamic_stop_loss or ll.stop_loss)
        if last and stop and last > 0:
            dist = (last - stop) / last * 100
            if dist <= _STOP_NEAR_PCT:
                actions.append(
                    f"接近参考卖出线：{ll.name}({ll.code}) 现价 {last:.2f}"
                    f" ｜ 卖出线 {stop:.2f} ｜ 相距 {dist:.1f}%")
                acted_codes.add(ll.code)
        if last and ll.take_profit and ll.take_profit > 0:
            gap = (ll.take_profit - last) / ll.take_profit * 100
            if gap <= _TARGET_NEAR_PCT:
                actions.append(
                    f"接近目标位：{ll.name}({ll.code}) 现价 {last:.2f}"
                    f" ｜ 目标位 {ll.take_profit:.2f} ｜ 相距 {gap:.1f}%")
                acted_codes.add(ll.code)
    for ll in pending:
        if ll.last_price and ll.entry_low and ll.last_price > 0:
            dist = abs(ll.last_price - ll.entry_low) / ll.last_price * 100
            if dist <= _TOUCH_NEAR_PCT:
                actions.append(
                    f"触价临近：{ll.name}({ll.code}) 现价 {ll.last_price:.2f}"
                    f" ｜ 区间下沿 {ll.entry_low:.2f} ｜ 相距 {dist:.1f}%")
                acted_codes.add(ll.code)

    lines.extend(actions)
    rest = len(tracked) - len(acted_codes & {ll.code for ll in tracked})
    # 一致行: 无 consistency_check 数据支撑时不得声称「理由仍然成立」(批复 honesty)
    if rest > 0 or not actions:
        lines.append(f"其余 {rest} 只无待办，持续监控中")
    return lines


def _label(ll: Lifeline) -> str:
    return f"{ll.name}({ll.code})" if ll.name else ll.code


def _ret(ll: Lifeline):
    return ll.total_return_pct if ll.total_return_pct is not None else ll.net_return_pct


def _held_summary(held: list[Lifeline]) -> str:
    """持有中组头行: 计数 + 最佳/最差 (浮盈记录缺失则略, 不编数)。"""
    scored = sorted(
        ((ll.unrealized_pnl_pct, ll) for ll in held
         if isinstance(ll.unrealized_pnl_pct, (int, float))),
        key=lambda x: x[0], reverse=True)
    extremes = ""
    if scored:
        best_p, best = scored[0]
        worst_p, worst = scored[-1]
        extremes = (f"（最佳 {best_p:+.1f}% {best.name}"
                    f"，最差 {worst_p:+.1f}% {worst.name}）")
    return f"【持有中 · 持续监控】{len(held)} 只触价买入后在场{extremes}"


def _exited_lines(exited: list[Lifeline]) -> list[str]:
    """已退出组: 计数 + 胜率 + 盈亏 Top3/Bottom3 各一行。"""
    with_ret = sorted(
        ((r, ll) for ll in exited if (r := _ret(ll)) is not None),
        key=lambda x: x[0], reverse=True)
    lines = []
    win = ""
    if with_ret:
        win_rate = sum(1 for r, _ in with_ret if r > 0) / len(with_ret) * 100
        win = (f"，胜率 {win_rate:.0f}%"
               f"（有盈亏记录 {len(with_ret)}/{len(exited)} 只）")
    lines.append(f"【已退出】{len(exited)} 只已退出{win}")
    if with_ret:
        top3 = with_ret[:3]
        lines.append("  盈亏前三：" + " ｜ ".join(
            f"{ll.name} {r:+.1f}%" for r, ll in top3))
        bottom3 = [x for x in with_ret[-3:] if x[0] < 0]
        if bottom3:
            lines.append("  亏损前三：" + " ｜ ".join(
                f"{ll.name} {r:+.1f}%" for r, ll in
                sorted(bottom3, key=lambda x: x[0])))
    return lines


def _pending_lines(pending: list[Lifeline], as_of: str,
                   top_pick_codes: set) -> list[str]:
    """未触价组: 一行计数 + 仅列有效余期 ≤2 天者; Top3 已推荐的不重复 (铁原则2)。"""
    due = [ll for ll in pending
           if ll.valid_until and 0 <= _days_left(ll.valid_until, as_of) <= _DUE_SOON_DAYS]
    due_visible = [ll for ll in due if ll.code not in top_pick_codes]
    due_dup = [ll for ll in due if ll.code in top_pick_codes]
    seg = ""
    if due_visible:
        names = "、".join(
            f"{ll.name}（余 {_days_left(ll.valid_until, as_of)} 天）"
            for ll in sorted(due_visible,
                             key=lambda x: _days_left(x.valid_until, as_of)))
        seg = f" ｜ 余期 ≤{_DUE_SOON_DAYS} 天需决策：{names}"
    if not due_visible and due_dup:
        seg = " ｜ 余期临界的候选已列入明日推荐，不重复展示"
    return [f"【未触价 · 计划继续】{len(pending)} 只候选全天未入买入区间"
            f"，继续跟踪{seg}"]


def _expired_lines(expired: list[Lifeline]) -> list[str]:
    """已过期组: 一行计数 (诚实水位: 绝不静默消失)。"""
    return [f"【已过期 · 计划作废】{len(expired)} 只候选到期未触发买入，计划作废"]


class EveningStorylineSection(Section):
    name = "evening_storyline"

    def build(self, context) -> SectionResult | None:
        try:
            db = getattr(context.data_layer, "db", None) if context.data_layer else None
            if db is None:
                return SectionResult(
                    title="🧵 今日候选故事线",
                    body="⚪ 数据缺口（行情库不可用），故事线暂不可用",
                    name=self.name)
            conn = db.get_connection()
            lifelines = build_lifelines(conn)

            bought, pending, expired = [], [], []
            for ll in lifelines:
                if "ST" in (ll.name or "").upper():
                    continue
                if ll.entry_date or ll.status in (list(_HELD_STATUSES) + ["exited"]):
                    bought.append(ll)
                elif is_open(ll, context.date):
                    pending.append(ll)
                else:
                    expired.append(ll)
            held = [ll for ll in bought if ll.status != "exited"]
            exited = [ll for ll in bought if ll.status == "exited"]

            if not (bought or pending or expired):
                return SectionResult(
                    title="🧵 今日候选故事线",
                    body="⚪ 今日无候选生命线，只看不动",
                    name=self.name)

            top_pick_codes = pick_codes(conn, context.date)

            lines = []
            if held:
                lines.append(_held_summary(held))
                lines.extend(_holdings_monitor(conn, lifelines))
            if exited:
                lines.extend(_exited_lines(exited))
            if pending:
                lines.extend(_pending_lines(pending, context.date,
                                            top_pick_codes))
            if expired:
                lines.extend(_expired_lines(expired))
            if not lines:
                lines.append("⚪ 今日无候选生命线，只看不动")
            return SectionResult(title="🧵 今日候选故事线",
                                 body="\n".join(lines),
                                 name=self.name,
                                 sources=["lifeline", "stock_pool"])
        except Exception as e:  # noqa: BLE001
            logger.warning(f"EveningStoryline build failed: {e}")
            return SectionResult(
                title="🧵 今日候选故事线",
                body="⚪ 数据缺口（读取异常），故事线暂不可用",
                name=self.name)
