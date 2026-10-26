# -*- coding: utf-8 -*-
"""AG-DEBATE-V2 第二段 — invalidation_conditions → 执行层条件单桥接 (2026-09-13)。

自家闭环: expert 报告的失效条件字段 (第一段生成面) → EX-4 advise 消费面
(执行层三态条件单四件套的警示层补充)。

语义定位 (派单口径+第一段设计):
  / 失效条件 = **警示层非否决层** — 不改三态判定 (buy_now 仍 buy_now),
    以 invalidation_warnings 结构化附出, 供报告/复盘/晨验面读。
  / 格式对齐执行层条件单: {source_expert, condition} 结构化条目
    (非原文本堆), JSON 可序列化。
  / 跨专家聚合去重 (同条件文本唯一), 截 8 条防爆。

接线形态: advise_with_invalidation(db, code, ..., expert_reports) =
  EX-4 原版 advise() + invalidation_warnings 附面 — 原判定零改动,
  下游 (报告明日推荐段/周报) 读 warnings 自愿消费。
"""
from __future__ import annotations

import re

from common.logging import get_logger

logger = get_logger("agent.advisor.invalidation_bridge")

_MAX_WARN = 8  # 警示条目上限 (防爆 — 报告面 token 预算)


def collect_invalidation_warnings(expert_reports) -> list[dict]:
    """跨专家失效条件聚合 → 结构化警示条目 (去重, 截 8 条)。

    格式对齐执行层条件单四件套的可读形态:
      {"source_expert": "tech", "condition": "跌破 9.5 支撑线"}
    """
    warns: list[dict] = []
    seen: set[str] = set()
    for r in (expert_reports or []):
        for cond in (getattr(r, "invalidation_conditions", None) or []):
            text = str(cond).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            warns.append({"source_expert": getattr(r, "expert_name", "?"),
                          "condition": text[:160]})
            if len(warns) >= _MAX_WARN:
                return warns
    return warns


def advise_with_invalidation(db, code: str, last_price: float = 0.0,
                             today: str = "", expert_reports=None) -> dict:
    """EX-4 advise() + 失效条件警示面 (原判定零改动)。

    三态判定完全走原版 advise (risk 否决/过期/价区逻辑不动);
    失效条件只作 invalidation_warnings 附加输出 — 警示非否决。
    """
    from execution_layer.advisor.recommendation_advisor import advise
    out = advise(db, code, last_price=last_price, today=today)
    warns = collect_invalidation_warnings(expert_reports)
    if warns:
        out["invalidation_warnings"] = warns
        # 观察单留痕: debug 级 (警示层不刷 INFO — 报告面才是消费主场)
        logger.debug(f"invalidation_bridge {code}: {len(warns)} 条失效警示附出")
    return out


# ---------------------------------------------------------------------------
# 正则预留: 「跌破 X」价格线提取 (第二段后续面 — 现版只透传不解析;
# 三期若要把警示升格为盘前扫描键, 在此扩 _PRICE_RE 抽数值)
# ---------------------------------------------------------------------------
_PRICE_RE = re.compile(r"跌破\s*([0-9]+(?:\.[0-9]+)?)")
