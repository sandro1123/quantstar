# -*- coding: utf-8 -*-
"""execution_layer/tracking/confidence_rules.py — 信心降档映射规则引擎 (REC-M4-followup)。

统筹设计定稿 (09-24 三裁):
a) 每腿一档封顶 + 多腿取最低, 单次最多降两档;
b) rules_hash 格式: `{人话原因}｜rules:{hash前8位}` (审计可正则解析);
c) **红线: 宁可降档不可假装高** —— 腿判据取不到数据 (非 ok 的任何状态) 一律
   按腿缺降档, 不得默认健康 (诚实水位延伸, 讨论#3 定谳)。

六条规则 (初版定稿, 统筹已批):
R1 LLM 辩论腿缺 → 降一档
R2 K线/估值腿缺 → 降一档
R3 资金流腿缺 → 降一档
R4 news_intel 腿缺 → 降一档
R5 多腿缺 (>=2) → 直接低档 (取最低不叠加)
R6 全腿齐 + 高置信 → 高 (不降, 无命中)

留痕: 命中写 claim {source:{kind:"rule", ref:"rule_id=..", value:"high→mid"}}
—— 与出口硬闸同构 (可 JOIN rule_id 回放)。规则本体是有序常量 + hash 锚:
规则集变更 → hash 变 → confidence_reason 尾部 hash 可审计「哪版规则算的」。

信心分档语义权威 = backtest_layer.evaluation.agent_eval.confidence_tier
(经 execution_layer.tracking.confidence 薄封装转调, 不重写阈值)。
"""
from __future__ import annotations

import hashlib
import json

# ---- 腿状态白名单 ----
LEG_HEALTHY = "ok"          # 显式健康 (唯一放行值)
# 其余任何状态值 (missing / judge_unavailable / 未来新增未知值) 一律按腿缺
# —— 红线: 白名单外默认健康 = 假装高。

_LEGS = ("llm_debate", "kline_valuation", "moneyflow", "news_intel")

# 有序规则表 (初版定稿; 规则变更 → _rules_hash 变 → 可审计)
RULES = [
    {"id": "R5", "trigger": "multi_leg_missing",  "effect": "to_low",
     "desc": "多腿缺(>=2)直接低档 (取最低不叠加)"},
    {"id": "R1", "trigger": "llm_debate_missing", "effect": "down_one",
     "desc": "LLM 辩论腿缺降一档 (判定靠规则腿, 缺 LLM 论证)"},
    {"id": "R2", "trigger": "kline_valuation_missing", "effect": "down_one",
     "desc": "K线/估值腿缺降一档 (关键证据面薄)"},
    {"id": "R3", "trigger": "moneyflow_missing",  "effect": "down_one",
     "desc": "资金流腿缺降一档 (资金佐证缺)"},
    {"id": "R4", "trigger": "news_intel_missing", "effect": "down_one",
     "desc": "news_intel 腿缺降一档 (题材佐证缺)"},
]

_TIER_ORDER = ("low", "mid", "high")


def _rules_hash(rules: list[dict]) -> str:
    """规则集指纹 (sha256 前 8 位): 同集同 hash, 规则变更 hash 变。"""
    raw = json.dumps(rules, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]


def _missing_legs(leg_status: dict) -> list[str]:
    """缺腿清单 (红线语义, 两分):
    - 显式传入且非 ok (missing/judge_unavailable/未知新值) → 腿缺 (红线:
      白名单外默认健康 = 假装高);
    - **未传入的腿 → 保守跳过不判** (不臆断: 调用方没提供健康面 ≠ 腿有病;
      判据都取不到的腿调用方会显式传 judge_unavailable)。"""
    missing = []
    for leg in _LEGS:
        if leg not in leg_status:
            continue  # 未传入: 不臆断
        if leg_status[leg] != LEG_HEALTHY:
            missing.append(leg)
    return missing


def _down_one(tier: str) -> str:
    i = _TIER_ORDER.index(tier)
    return _TIER_ORDER[max(0, i - 1)] if i > 0 else tier


def apply_rules(base_confidence: float | None, leg_status: dict) -> dict:
    """按规则集计算最终信心档位, 返回 {tier, hits, confidence_reason}。

    hits: [{rule_id, claim}] — claim 与出口硬闸同构
    ({source:{kind:"rule", ref:"rule_id=..", value:"srcTier→dstTier"}})。
    """
    from execution_layer.tracking.confidence import confidence_tier_for_tracking

    base_tier = confidence_tier_for_tracking(base_confidence)
    missing = _missing_legs(leg_status or {})

    hits: list[dict] = []
    tier = base_tier

    if tier in ("high", "mid") and missing:
        if len(missing) >= 2:
            # R5: 多腿缺直接低档 (取最低不叠加)
            src = tier
            tier = "low"
            hits.append({
                "rule_id": "R5",
                "claim": {"source": {"kind": "rule", "ref": "rule_id=R5",
                                     "value": f"{src}→{tier} (缺腿: {','.join(missing)})"}},
            })
        else:
            # R1-R4: 单腿缺降一档 (每腿一档封顶)
            rid = {"llm_debate": "R1", "kline_valuation": "R2",
                   "moneyflow": "R3", "news_intel": "R4"}[missing[0]]
            src = tier
            tier = _down_one(tier)
            hits.append({
                "rule_id": rid,
                "claim": {"source": {"kind": "rule", "ref": f"rule_id={rid}",
                                     "value": f"{src}→{tier} ({missing[0]} 腿缺)"}},
            })

    # 人话原因 (信心可低不可假装高: 缺腿如实陈述)
    if hits:
        reason = f"信心降档: {hits[-1]['claim']['source']['value']}"
    else:
        reason = f"信心 {base_tier}: 全数据腿健康" if missing == [] else \
                 f"信心 {base_tier}: 判据取不到但基档已低"
    reason = f"{reason}｜rules:{_rules_hash(RULES)}"

    return {"tier": tier, "hits": hits, "confidence_reason": reason}
