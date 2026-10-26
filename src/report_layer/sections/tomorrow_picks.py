# -*- coding: utf-8 -*-
"""明日推荐 Top3 (宪法 #1 行动面收敛 · TASK_20260922_01 Phase B 批复裁决1)。

Top N=3: composite (initial_score) 降序取 3 只, 宁缺毋滥 — 凑不齐就少报,
不写满。每只五件套齐: 为什么选它 / 在哪买 / 什么情况买 / 何时卖 / 信心与折扣
(综合分+审批状态+数据可信度提示)。

理由过单点闸 report_reason._pick_reason: 排名模板永不进卡 (批复裁决3),
显式标「量化评分入选，个股依据生成中」；advisor_text 落地后零改动升级。
第 4 只起折叠计数行「另有 N 只候选（综合分 X~Y），全量见深钻页 P0」,
全量表落 marimo P0 (5 分钟层)。
数据缺日诚实「暂无推荐（数据积累中）」不编造。

1b 嫁接（22:05 窗冲突合案）：「为什么选它」agent_memory 论证
（advisor 忠实性守卫加工，只读零副作用）优先于 _pick_reason；
守卫拒绝/无当日论证 → _pick_reason 诚实占位，排名模板仍永不进卡。
论证来源升级不改结构：advisor_text 落地后两链同源。
"""
from __future__ import annotations

from common.logging import get_logger
from report_layer.sections.base import Section, SectionResult
from report_reason import _pick_reason_card
from report_layer.advisor import load_argument, persist_advisor, rewrite_to_advisor
from report_layer.advisor.trace_conn import resolve_trace_conn

logger = get_logger("report.section.tomorrow_picks")

TOP_N = 3
_FETCH_CAP = 30  # 候选抓取上限 (Top3 + 折叠计数)

# G2 修 (2026-10-22): 原硬编码 ('watch','pending_entry') 漏了 'active' ——
# 当日晚报「明日推荐」渲染空, 而库内明明入池 2 只 (status=active)。
# 真病不是漏一个状态, 是 **权威常量 ALIVE_STATUSES 定义了却没人用**
# (stock_pool.py:27, 全仓仅定义处+一处内部校验引用)。
# 现改为引用权威常量 + 占位符, 防后人再硬编码一份。
from execution_layer.core.stock_pool import ALIVE_STATUSES as _ALIVE

_ALIVE_PLACEHOLDERS = ",".join("?" * len(_ALIVE))
_CANDIDATE_SQL = (
    "SELECT code, name, entry_low, entry_high, stop_loss, take_profit,"
    " initial_score, added_reason FROM stock_pool"
    " WHERE status IN (" + _ALIVE_PLACEHOLDERS + ")"
    " AND (valid_until IS NULL OR valid_until >= ?)"
    " ORDER BY initial_score DESC, code LIMIT ?")

_REVIEW_STATUS_CN = {"pending": "待审批", "approved": "已批准",
                     "rejected": "已驳回", "invalid": "已失效",
                     "not_queued": "未送审"}

_HEALTH_CN = {"green": "绿色", "yellow": "黄色", "red": "红色"}
_HEALTH_HINT = {"yellow": "，相关推荐信心关注",
                "red": "，今日推荐信心打折"}


def _candidate_rows(conn, as_of: str, cap: int = _FETCH_CAP):
    # 参数顺序: 先状态集合(升序稳定), 再 as_of, 再 cap
    params = tuple(sorted(_ALIVE)) + (as_of, cap)
    return conn.execute(_CANDIDATE_SQL, params).fetchall()


def pick_codes(conn, as_of: str, top_n: int = TOP_N) -> set:
    """明日推荐 Top N 代码集合 — 故事线分组去重用 (铁原则2: 一票只在一组)。"""
    try:
        return {r[0] for r in _candidate_rows(conn, as_of)[:top_n]}
    except Exception:  # noqa: BLE001
        return set()


def _review_status_cn(conn, code: str) -> str:
    """审批状态中文 (展示层映射, human_review_queue 缺表 → 未送审, 不崩)。"""
    try:
        row = conn.execute(
            "SELECT status FROM human_review_queue WHERE code = ?"
            " ORDER BY created_at DESC LIMIT 1", (code,)).fetchone()
    except Exception:  # noqa: BLE001
        return "未送审"
    if not row:
        return "未送审"
    return _REVIEW_STATUS_CN.get(str(row[0]), str(row[0]))


def _health_discount(conn) -> str:
    """数据可信度提示 (承接 oneliner 定谳): 红/黄给信心折扣, 缺表/绿不追加。"""
    try:
        row = conn.execute(
            "SELECT overall_status FROM health_history"
            " ORDER BY check_time DESC LIMIT 1").fetchone()
    except Exception:  # noqa: BLE001
        return ""
    if not row or not row[0]:
        return ""
    health = str(row[0])
    hint = _HEALTH_HINT.get(health)
    if not hint:
        return ""
    return f" ｜ 数据可信度 {_HEALTH_CN.get(health, health)}{hint}"


class TomorrowPicksSection(Section):
    name = "tomorrow_picks"

    def build(self, context) -> SectionResult | None:
        try:
            db = getattr(context.data_layer, "db", None) if context.data_layer else None
            sl = getattr(context, "strategy_layer", None)
            if db is None or sl is None:
                return SectionResult(
                    title="📌 明日推荐",
                    body="暂无推荐（策略数据积累中）", name=self.name)
            conn = db.get_connection()
            # 1b: 论证读连接（取不到=回落 _pick_reason，不炸渲染）
            try:
                mem_conn = db.get_connection()
            except Exception:  # noqa: BLE001
                mem_conn = None
            # 1b: 守卫留痕写连接（审计 P0③：渲染链必须传 trace_db）
            trace_conn = resolve_trace_conn(context)
            rows = _candidate_rows(conn, context.date)
            if not rows:
                return SectionResult(
                    title="📌 明日推荐", body="暂无推荐（候选池积累中）",
                    name=self.name)
            picks = rows[:TOP_N]
            lines = [f"模拟盘 · 明日推荐（{len(picks)} 只）"]
            health = _health_discount(conn)
            for (code, name, low, high, stop, take, score, reason) in picks:
                label = f"{name}({code})" if name else code
                lines.append(f"◆ {label}")
                # 为什么选它: 1b advisor 链优先；拒绝/无论证 → _pick_reason 闸
                why = None
                try:
                    arg = load_argument(code, context.date, mem_conn)
                    if arg is not None:
                        res = rewrite_to_advisor(
                            arg, llm=getattr(context, "llm_service", None),
                            trace_db=trace_conn, date=context.date)
                        if res.sentences:
                            why = res.text.replace("\n", " ")
                            persist_advisor(conn, res)
                except Exception:  # noqa: BLE001
                    why = None
                if not why:
                    # P0-1 (2026-09-28): 论证卡 — rationale 通电后自动升级多字段卡
                    # (零改动消费方: 当前 rationale 0 行降级为单句, 通电后自动升级)
                    # 判例#132: 传 code 才能读 rationale 论证卡 (Day3 补锚)
                    why = _pick_reason_card({"code": code, "added_reason": reason}, conn=conn, date=context.date)
                lines.append(f"  为什么选它：{why}")
                if low and high:
                    lines.append(f"  在哪买：建议价区 {low:.2f} ~ {high:.2f}")
                else:
                    lines.append("  在哪买：价区待盘中确认（竞价后核）")
                lines.append("  什么情况买：回踩价区企稳且量能配合时介入")
                if stop and take:
                    lines.append(f"  何时卖：目标 {take:.2f}，参考卖出线 {stop:.2f}")
                else:
                    lines.append("  何时卖：目标位与参考卖出线随入场后动态设定")
                score_s = f"{score:.3f}" if isinstance(score, (int, float)) else "评分生成中"
                review = _review_status_cn(conn, code)
                lines.append(f"  信心与折扣：综合分 {score_s} ｜ 审批 {review}{health}")
            rest = rows[TOP_N:]
            if rest:
                scores = [r[6] for r in rest
                          if isinstance(r[6], (int, float))]
                seg = (f"综合分 {scores[0]:.2f}~{scores[-1]:.2f}"
                       if scores else "综合分见深钻页")
                lines.append(
                    f"另有 {len(rest)} 只候选（{seg}），全量见深钻页 P0")
            return SectionResult(title="📌 明日推荐",
                                 body="\n".join(lines), name=self.name,
                                 sources=["stock_pool", "agent_memory"])
        except Exception as e:
            logger.warning(f"TomorrowPicksSection build failed: {e}")
            return SectionResult(title="📌 明日推荐",
                                 body="暂无推荐（数据积累中）", name=self.name)
