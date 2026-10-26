# -*- coding: utf-8 -*-
"""AG-DEBATE-V2 第一段 — 2 轮制辩论编排 (2026-09-12 夜班, 派单 sm-64d3f480)。

细聊定谳⑥: deep 档辩论 1 轮→2 轮制 — 第 1 轮各专家独立出意见 → 第 2 轮
互看对方 key_points 修订 → 裁决官终审。invalidation_conditions (条件失效声明)
接执行层三态条件单 (EX-4 order_plan 下游)。

本模块 = 编排核 (纯函数面, 可单测; _run_debate 消费):
  - rounds_config(): config 读 rounds, clamp(1,2), 出厂 2
  - compose_round2_data(): 第 2 轮喂料 = 原数据 + peer_review (第 1 轮全专家摘要)
  - run_debate(): 两轮跑专家 (每轮新实例防状态残留), 返回 {rounds_run,
    round1_reports, round2_reports} — 裁决官收 round2 (修订后) 为终版。

三重证自查 (防建了没通电):
  / 编排可跑 — test_two_round_orchestration_dry_run 假库全程
  / 配置在册 — config/defaults/agent.yaml agent.debate_rounds=2
  / 消费面能读 — _run_debate 接线 (本卡施工面: agent_layer.py 真改)
"""
from __future__ import annotations

from common.logging import get_logger

logger = get_logger("agent.debate.rounds_orchestrator")

DEFAULT_ROUNDS = 2
MAX_ROUNDS = 2   # V2 第一段: 上界 2 (后续段扩 3 时改此处)


def rounds_config(cfg: dict | None) -> int:
    """config 读轮次 — clamp(1, MAX), 非法值回落出厂 2。"""
    try:
        n = int((cfg or {}).get("debate_rounds", DEFAULT_ROUNDS))
    except (TypeError, ValueError):
        return DEFAULT_ROUNDS
    if n < 1:
        return DEFAULT_ROUNDS   # 0/负数视为非法, 回落出厂 (非 clamp 到 1)
    return min(n, MAX_ROUNDS)


def compose_round2_data(base_data: dict, round1_reports: list) -> dict:
    """第 2 轮喂料 — 原数据保留 + peer_review 摘要段 (互看对方 key_points)。"""
    lines = []
    for r in round1_reports:
        pts = "; ".join((r.key_points or [])[:3])
        tier = getattr(r, "tier_used", "rules")
        lines.append(f"[{r.expert_name}|{tier}] score={r.score:+.2f} {pts}")
    data = dict(base_data)
    data["peer_review"] = "\n".join(lines)
    return data


def run_debate(base_data: dict, expert_classes: list,
               config: dict | None = None,
               provider_manager=None, db=None) -> dict:
    """两轮编排核 — 每轮专家新实例 (防第 1 轮状态残留污染第 2 轮)。

    返回 {rounds_run, round1_reports, round2_reports};
    rounds=1 时 round2_reports=[] (裁决面只用 round1)。
    db 透传 set_memory_db (记忆层注入线, 缺席零注入)。
    """
    rounds = rounds_config(config)
    r1 = _run_round(expert_classes, base_data, db)
    if rounds < 2:
        return {"rounds_run": 1, "round1_reports": r1, "round2_reports": []}
    round2_data = compose_round2_data(base_data, r1)
    r2 = _run_round(expert_classes, round2_data, db)
    logger.info(
        f"AG-DEBATE-V2: 2 轮制毕 ({len(r1)} 专家×2 轮, "
        f"round2 peer_review {len(round2_data['peer_review'])} 字)")
    return {"rounds_run": 2, "round1_reports": r1, "round2_reports": r2}


def _run_round(expert_classes: list, data: dict, db) -> list:
    """单轮: 全专家各出一份 ExpertReport; 单专家失败 warning 不炸链
    (AG-COMPOSE 先例: 一损不全灭)。"""
    reports = []
    for cls in expert_classes:
        try:
            expert = cls()
            if db is not None and hasattr(expert, "set_memory_db"):
                expert.set_memory_db(db)
            reports.append(expert.analyze(data))
        except Exception as e:
            logger.warning(f"AG-DEBATE-V2: expert {getattr(cls, 'name', cls)} "
                           f"轮内失败 (不炸链): {e}")
    return reports
