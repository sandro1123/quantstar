# -*- coding: utf-8 -*-
"""推荐理由单点闸 (TASK_20260922_01 Phase B · 批复裁决3)。

唯一职责: 从一行推荐数据里取「为什么选它」的用户可读理由 —
  1. advisor_text / recommendation_rationale 字段存在且非空 → 优先采用
     (REC 真理由落地之日零改动自动升级, 接口先对);
  2. added_reason 命中排名模板 (候选池第 N 位 / 综合评分排名 / 综合得分 x.xxx)
     → 永不渲染, 降级「量化评分入选，个股依据生成中」(诚实水位, 不充数);
  3. 其余非空 added_reason → 原样采用;
  4. 全空 → 「个股依据生成中」。

消费方: 晚报 tomorrow_picks / DM 卡 build_marimo_report.build_dm_card —
同一函数, 口径统一 (批复裁决3)。

放 src 顶层而非 report_layer 包内: DM 卡脚本跑在 marimo venv (缺 pydantic
等依赖), 无法 import report_layer 包; 两侧消费者都要求零依赖可达本函数。
"""
from __future__ import annotations

import re

# 排名模板正则 (批复 §原因说明: 存量 stock_pool.added_reason 全是此类)
RANK_PHRASE_RE = re.compile(r"候选池第|综合评分排名|综合得分\s*\d+\.\d+")

GENERATING_TEXT = "量化评分入选，个股依据生成中"
NO_REASON_TEXT = "个股依据生成中"

_PRIORITY_FIELDS = ("advisor_text", "recommendation_rationale")


def _field(row, key):
    """宽容取字段: dict/pandas Series/sqlite3.Row/普通对象均可。"""
    if row is None:
        return None
    get = getattr(row, "get", None)
    if callable(get):
        try:
            return get(key)
        except Exception:
            pass
    try:
        return row[key]
    except Exception:
        pass
    return getattr(row, key, None)


def is_rank_phrase(reason) -> bool:
    return bool(reason) and bool(RANK_PHRASE_RE.search(str(reason)))


def _expand_rationale(val) -> str | None:
    """rationale 对象展开 (09-29 降级序 v2, 用户视角席三洞修②)。

    rationale 是 dict/对象非字符串 — isinstance(val,str) 失败跳过落排名话术
    (通了电的 rationale 读不出来)。展开序: reasoning (人话) → key_points
    首条 (JSON list 或文本)。忠实性守卫: verdict 不回传 (调用方按 verdict
    渲染方向, 此处只取理由文本)。
    """
    if val is None:
        return None
    # 裸字符串兼容 (旧格式 rationale 直存文本 — 第一档语义直用)
    if isinstance(val, str):
        return val.strip() or None

    def _get(obj, key):
        if isinstance(obj, dict):
            return obj.get(key)
        return getattr(obj, key, None)

    reasoning = _get(val, "reasoning")
    if isinstance(reasoning, str) and reasoning.strip():
        return reasoning.strip()
    kp = _get(val, "key_points")
    if isinstance(kp, str) and kp.strip():
        import json as _json
        try:
            parsed = _json.loads(kp)
            if isinstance(parsed, list) and parsed:
                first = str(parsed[0]).strip()
                if first:
                    return first
        except Exception:
            first = kp.split(",")[0].strip().strip("[]\"'")
            if first:
                return first
    if isinstance(kp, (list, tuple)) and kp:
        first = str(kp[0]).strip()
        if first:
            return first
    return None


def _pick_reason(row) -> str:
    """取推荐理由 (降级序 v2, 09-29)。返回值永不为空串。

    降级序 (保铁闸「永不回落排名话术」):
      advisor_text → rationale 展开 → added_reason (非排名话术) → 诚实占位。
    """
    advisor = _field(row, "advisor_text")
    if isinstance(advisor, str) and advisor.strip():
        return advisor.strip()
    rationale = _field(row, "recommendation_rationale")
    expanded = _expand_rationale(rationale)
    if expanded:
        return expanded
    added = _field(row, "added_reason")
    added = str(added).strip() if added is not None else ""
    score = _field(row, "initial_score")
    try:
        score = float(score) if score is not None else None
    except (TypeError, ValueError):
        score = None
    if not added:
        return NO_REASON_TEXT
    if is_rank_phrase(added):
        # 机制性互斥：deep_top_n 只覆盖前 N 只，剩余票的 rationale 缺。
        # 有初始分数时给诚实分数理由，不占位充数；无分数再走占位。
        if score is not None:
            return f"综合分 {score:.2f}，量化评分入选，个股依据生成中"
        return GENERATING_TEXT
    return added

def _pick_reason_card(row, agent_review=None, conn=None, date=None) -> str:
    """论证卡输出（P0-1, 2026-09-28）——多字段结构（为什么/信心/失效条件/风险）。

    「砍规格=纯规则模板」：字段模板（不是句式模板）——
    从 agent_reviews 的 key_points/invalidation_conditions/confidence 直接装配，
    不需要 LLM。结论/数字/因子 ⊆ 原料不可越（守卫管），表达风格可 LLM（守卫不管风格）。

    契约：
    - agent_review 有 key_points/invalidation_conditions → 装配成多字段论证卡
    - 否则降级为 _pick_reason 的单句输出（零改动既有行为）
    - 输出无内部标记（[...]【...】不换行）
    """
    # agent_review 为 None 时，若传了 conn+date，内部读 rationale 表
    # (rationale 优先/agent_memory 回落, load_argument 已有)。
    # 当前 rationale 表 0 行 → 降级为 _pick_reason 单句;
    # rationale 通电后自动升级为论证卡 (零改动消费方)。
    if agent_review is None and conn is not None and date:
        try:
            from report_layer.advisor import load_argument
            code = _field(row, "code") or ""
            if code:
                agent_review = load_argument(code, date, conn)
        except Exception:
            agent_review = None

    if agent_review is None:
        return _pick_reason(row)

    key_points = getattr(agent_review, "key_points", None) or []
    invalidation = getattr(agent_review, "invalidation_conditions", None) or []
    confidence = getattr(agent_review, "confidence", None)
    confidence_tier = getattr(agent_review, "confidence_tier", None)
    # verdict = getattr(agent_review, "verdict", "") or ""

    if not key_points:
        # 无 key_points → 降级为单句（诚实水位，不空）
        return _pick_reason(row)

    # 字段模板装配（零 LLM，纯规则）
    parts = []
    # 为什么（前两个 key_points）
    if len(key_points) >= 2:
        parts.append(f"为什么：{key_points[0]}，{key_points[1]}")
    else:
        parts.append(f"为什么：{key_points[0]}")

    # 信心（降档后值优先，不用 raw confidence）
    if confidence_tier:
        parts.append(f"信心：{confidence_tier}")
    elif confidence is not None:
        parts.append(f"信心：{confidence:.0%}")

    # 失效条件（有值用值，空则默认兜底）
    if invalidation:
        parts.append(f"失效条件：{invalidation[0]}")
    else:
        parts.append("失效条件：跌破止损线")

    # 风险（第二个 invalidation 或默认）
    if len(invalidation) >= 2:
        parts.append(f"风险：{invalidation[1]}")
    elif invalidation:
        parts.append("风险：需跟踪量能变化")
    else:
        parts.append("风险：需跟踪量能变化")

    card = " | ".join(parts)
    # 清洗内部标记（[..]【..】换行）
    import re as _re
    card = _re.sub(r"\[[^\]]*\]|【[^】]*】", " ", card)
    card = _re.sub(r"\s+", " ", card).strip()
    return card

