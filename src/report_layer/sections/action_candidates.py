"""R1: 🎯 今日行动候选 Section — 行动卡组 (设计稿-C §2.2, D-277 七要素)。

候选集合 = 生命线 ∈ {pending_entry, watch 且入场就绪} 且 valid_until 未过期
(D7 连续性窗口: is_open)。每只四行固定结构:
    标题行  🟢/🔵 {名称}({code}) · {行业}   (绿=可买, 蓝=观察)
    行动行  进入模拟买入区间: 区间 ｜ 参考卖出线(约-x%) ｜ 参考止盈 ｜ 有效至(余N天)
              (P6 定调中性: 「可以买」祈使句退役, 2026-09-24 裁定#4)
    理由行  advisor_text 存在即用 (_pick_reason 冻结单点); 缺省=因子中文化一句话
              (复用 stock_picks._factor_cn, 禁因子英文缩写; 不用「生成中」占位替换)
    署名行  审批 {中文} · 置信 N% (内部来源名不裸漏; 生命线 ID 不进卡片正文)

Top N 收敛 (morning-topn-converge, 裁定#1 对齐晚报少即是多):
    Top 3 (initial_score 降序+code 决胜, 宁缺毋滥不写满);
    第 4 只起降一行折叠「另有 N 只候选，全量见深钻页 P0」。
措辞宪法 (F-20260828): 止损线→参考卖出线; 主句永远是「你可以做什么」。
诚实水位 (范式 7): 空池/全过期/数据缺口 → 「⚪ 今日无操作建议…只看不动」。
只读: 经 data_layer.db.get_connection() + build_lifelines, 零写入 (铁律#4)。
"""
from __future__ import annotations

import json
from datetime import date as _date

from common.logging import get_logger
from report_layer.lifeline import Lifeline, build_lifelines, is_open
from report_layer.sections.base import Section, SectionResult
from report_layer.sections.stock_picks import StockPicksSection
from report_layer.sections.tomorrow_picks import _review_status_cn
from report_reason import GENERATING_TEXT, NO_REASON_TEXT, _pick_reason_card

logger = get_logger("report.section.action_candidates")

# 入场就绪阈值 (与 watch_focus 一致: score > 0.6 才可入场)
_ENTRY_READY_SCORE = 0.6
# 裁定#1 (对齐晚报): Top 3 收敛, 宁缺毋滥; 第 4 只起折叠计数指深钻页 P0
_TOP_N = 3

_factor_cn_base = StockPicksSection()._factor_cn

# 生产 factor_scores 实测键 (Qlib alpha158 族, 2026-08-28 枚举 80 键) 的中文补全。
# _factor_cn_base 只覆盖 40 个策略层因子名; 此处补齐盘中因子键, 避免英文泄漏。
_FACTOR_CN_EXTRA = {
    "klow": "最低价相对", "klow2": "下影线", "kup": "最高价相对",
    "kup2": "上影线", "kmid": "中价相对", "kmid2": "实体中位",
    "klen": "K线长度", "ksft": "K线偏移", "ksft2": "K线偏移2",
    "roc5": "5日涨速", "roc10": "10日涨速", "roc20": "20日涨速",
    "roc60": "60日涨速",
    "sump5": "5日累计涨幅", "sump10": "10日累计涨幅", "sump20": "20日累计涨幅",
    "sumd5": "5日累计跌幅", "sumd10": "10日累计跌幅", "sumd20": "20日累计跌幅",
    "cntp5": "5日连跌天数", "cntp10": "10日连跌天数", "cntp20": "20日连跌天数",
    "cntd5": "5日连涨天数", "cntd10": "10日连涨天数", "cntd20": "20日连涨天数",
    "std5": "5日波动", "std10": "10日波动", "std20": "20日波动",
    "vstd5": "5日量能波动", "vstd10": "10日量能波动",
    "vma5": "5日量能均线", "vma10": "10日量能均线", "vma20": "20日量能均线",
    "ma5_dev": "5日均线偏离", "ma10_dev": "10日均线偏离",
    "corr5": "5日量价相关", "corr10": "10日量价相关",
    "cord5": "5日量价相关(收盘)", "cord20": "20日量价相关(收盘)",
    "rank5": "5日价格分位", "rank10": "10日价格分位",
    "max5": "5日最高", "min5": "5日最低",
    "rsi14": "RSI(14日)", "rsi6": "RSI(6日)", "rsi12": "RSI(12日)",
    "leverage_low": "低杠杆", "accrual_quality": "应计质量",
    "liquidity_illiquid": "非流动性", "turnover_ratio": "换手率",
    "concept_heat": "概念热度", "concept_count": "概念数量",
    "lhb_net_buy": "龙虎榜净买", "lhb_drama_score": "龙虎榜博弈",
    "analyst_consensus": "分析师共识", "moneyflow_main_net": "主力资金净流入",
    "pattern_technical": "技术形态", "momentum_5d_reversal": "5日反转",
    "volatility_reversal": "波动反转", "seasonality_same_month": "同月季节性",
    "growth_combined": "综合增长",
}


def _factor_cn(name: str) -> str | None:
    """因子中文化: 基础 40 键 + Qlib 补全; 未知键返回 None (不泄漏英文)。"""
    if name in _FACTOR_CN_EXTRA:
        return _FACTOR_CN_EXTRA[name]
    mapped = _factor_cn_base(name)
    # _factor_cn_base 对未知键原样返回英文名 → 视为未映射
    return None if mapped == name else mapped


def _honest(reason: str) -> SectionResult:
    """无推荐日诚实模板 (样张 §二 + 范式 7)。"""
    return SectionResult(
        title="🎯 今日行动候选",
        body=f"⚪ 今日无操作建议：{reason}，今日动作=只看不动",
        name="action_candidates")


def _days_left(valid_until: str, as_of: str) -> int:
    try:
        return (_date.fromisoformat(valid_until)
                - _date.fromisoformat(as_of)).days
    except ValueError:
        return 0


def _diff_line(conn, as_of: str) -> str | None:
    """较昨日候选差异行 (09-28 派单, 统筹裁 a: action_type 组口径)。

    口径 (锚 action_type 组非 status_change 裸计数, 治虚高):
      新进 = add_to_watch + status_change(reason 含 →pending_entry)
      移出 = remove_from_watch + cancel_entry  (正向变迁类退出,
             剔除 skip_entry/skip_exit 负面跳过)
      调价 = set_entry_range
    昨日 = as_of 前一自然日 (晨报 08:00 跑, 前一交易日变迁已在库)。
    previous_value/new_value 全 NULL (09-24/25 实查) — 精筛无锚, 锚 action_type。
    无差异 / 查询异常 → None (不渲染, 不静默当零)。
    """
    try:
        from datetime import date as _d, timedelta
        yesterday = (_d.fromisoformat(str(as_of)[:10]) - timedelta(days=1)).isoformat()
        rows = conn.execute(
            "SELECT action_type, reason FROM action_records "
            "WHERE substr(created_at, 1, 10) = ?", (yesterday,)).fetchall()
        if not rows:
            return None
        new_in = sum(1 for at, _ in rows if at == "add_to_watch")
        new_in += sum(1 for at, r in rows
                      if at == "status_change" and "pending_entry" in (r or ""))
        moved_out = sum(1 for at, _ in rows
                        if at in ("remove_from_watch", "cancel_entry"))
        repriced = sum(1 for at, _ in rows if at == "set_entry_range")
        if not (new_in or moved_out or repriced):
            return None
        return f"较昨日：新进 {new_in} 只｜移出 {moved_out} 只｜调价 {repriced} 只"
    except Exception:  # noqa: BLE001 — 差异行失败不拖垮主卡
        return None


def _is_st(name: str) -> bool:
    return "ST" in (name or "").upper()


def _reason_line(conn, code: str, as_of: str) -> str:
    """理由行: 最新 factor_scores 前三因子中文化 (复用 _factor_cn)。"""
    try:
        row = conn.execute(
            "SELECT factor_scores FROM factor_daily "
            "WHERE code=? AND date<=? ORDER BY date DESC LIMIT 1",
            (code, as_of)).fetchone()
        if not row or not row[0]:
            return ""
        scores = json.loads(row[0])
        top = sorted(scores.items(), key=lambda kv: abs(kv[1] or 0),
                     reverse=True)
        parts = []
        for k, v in top:
            if v is None or abs(v) < 0.01:
                continue
            cn = _factor_cn(k)
            if cn is None:
                continue  # 未知因子键不泄漏英文, 跳过
            parts.append(f"{cn} {v:+.2f}")
            if len(parts) >= 3:
                break
        if not parts:
            return ""
        return "理由：" + " ｜ ".join(parts)
    except Exception as e:
        logger.warning(f"reason line for {code} failed: {e}")
        return ""


def _advisor_reason(ll, conn=None, date=None) -> str:
    """理由行·advisor 优先 (裁定#2): 切 _pick_reason_card (Day 3 卡一)。
    advisor_text/recommendation_rationale 存在即用 (忠实性守卫: 顾问文只展示不加工);
    rationale 表 0 行 或其他论证缺 → 回落 _pick_reason 单句 (零行为改动)。
    不存在返回空串, 调用方回落因子中文化行 (不用「生成中」占位替换真理由)。

    conn/date 注入 rationale 表 读 面 (三条同样板 tomorrow_picks/exit_attribution):
    conn 缺 → 零改动 老行为; conn 有 → 升级为多字段论证卡 (可多 字段)。"""
    # 判例#132 参数维度接线断裂: _pick_reason_card 需 row["code"] 才能读 rationale,
    # 不传 code → 走降级 → 全占位(2026-10-03 Day3 验收实证 920394/688480)。
    why = _pick_reason_card({
        "code": getattr(ll, "code", "") or "",
        "advisor_text": getattr(ll, "advisor_text", None),
        "recommendation_rationale": getattr(ll, "recommendation_rationale", None)},
        conn=conn, date=date)
    return "" if why in (GENERATING_TEXT, NO_REASON_TEXT) else why


def _card(ll: Lifeline, conn, as_of: str) -> list[str]:
    """单只候选四行卡 (缺要素的行诚实省略, 不造假值)。"""
    icon = "🟢" if ll.status == "pending_entry" else "🔵"
    industry = f" · {ll.industry}" if ll.industry else ""
    lines = [f"{icon} {ll.name}({ll.code}){industry}"]
    action = []
    if ll.entry_low is not None and ll.entry_high is not None:
        action.append(f"进入模拟买入区间: {ll.entry_low:.2f}–{ll.entry_high:.2f}")
    if ll.stop_loss is not None:
        pct = ""
        if ll.entry_high:
            pct = f"（约 {(ll.stop_loss / ll.entry_high - 1) * 100:.1f}%）"
        action.append(f"参考卖出线: {ll.stop_loss:.2f}{pct}")
    if ll.take_profit is not None:
        action.append(f"参考止盈: {ll.take_profit:.2f}")
    if ll.valid_until:
        action.append(f"有效至 {ll.valid_until}（余 {_days_left(ll.valid_until, as_of)} 天）")
    if action:
        lines.append(" ｜ ".join(action))
    advisor = _advisor_reason(ll)
    if advisor:
        lines.append(f"理由：{advisor}")
    else:
        reason = _reason_line(conn, ll.code, as_of)
        if reason:
            lines.append(reason)
    review = _review_status_cn(conn, ll.code)
    conf = f"{ll.initial_score:.0%}" if ll.initial_score is not None else "未知"
    lines.append(f"审批 {review} · 置信 {conf}")
    lines.append("")
    return lines


class ActionCandidatesSection(Section):
    name = "action_candidates"

    def build(self, context) -> SectionResult | None:
        try:
            db = getattr(context.data_layer, "db", None) if context.data_layer else None
            if db is None:
                return _honest("数据缺口（行情库不可用）")
            conn = db.get_connection()
            lifelines = build_lifelines(conn)

            pool = [ll for ll in lifelines if ll.status in ("pending_entry", "watch")]
            if not pool:
                return _honest("候选池为空")

            candidates = []
            for ll in pool:
                if _is_st(ll.name):
                    continue  # ST 进回避卡, 不进买入位
                if not is_open(ll, context.date):
                    continue
                if ll.status == "watch" and (ll.initial_score or 0) <= _ENTRY_READY_SCORE:
                    continue  # 观察池未就绪只跟踪, 不发行动卡
                candidates.append(ll)
            if not candidates:
                return _honest("全部候选已过期")

            # 裁定#1: initial_score 降序 + code 决胜, Top 3 宁缺毋滥
            candidates.sort(key=lambda ll: (-(ll.initial_score or 0), ll.code))
            lines = [f"🎯 今日行动候选 ({len(candidates[:_TOP_N])} 只)"]
            _diff = _diff_line(conn, context.date)
            if _diff:
                lines.append(_diff)
            lines.append("=" * 40)
            for ll in candidates[:_TOP_N]:
                lines.extend(_card(ll, conn, context.date))
            rest = len(candidates) - _TOP_N
            if rest > 0:
                lines.append(f"另有 {rest} 只候选，全量见深钻页 P0")
            return SectionResult(title="🎯 今日行动候选",
                                 body="\n".join(lines).rstrip(),
                                 name="action_candidates",
                                 sources=["lifeline", "factor_daily"])
        except Exception as e:
            logger.warning(f"ActionCandidatesSection build failed: {e}")
            return _honest("数据缺口（读取异常）")
