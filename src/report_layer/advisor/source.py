# -*- coding: utf-8 -*-
"""模块1b 原料读取器 — 从 agent 层已留痕的 agent_memory 表读论证（只读零副作用）

契约条款3合规：1b 消费 agent 已产出的论证，不现场触发 agent 重算。
生产词表（AG-03）：推荐/不推荐/观望/警惕 → 内部语义域 buy/hold/avoid；
未知词表值 → 保守映射 avoid（禁 KeyError 穿透，空集显式报警同族）。
"""
from types import SimpleNamespace

from .models import RecommendationArgument
from .factor_extract import extract_factors
from .rationale_join import load_argument_from_rationale
from .text_domain import extract_numbers

import sqlite3

# AG-03 生产词表 → 内部语义域白名单
_VERDICT_MAP = {
    "推荐": "buy",
    "买入": "buy",
    "不推荐": "avoid",
    "卖出": "avoid",
    "规避": "avoid",
    "观望": "hold",
    "警惕": "hold",
    "持有": "hold",
}
_CONSERVATIVE_VERDICT = "avoid"

# 闸2 原料数字域提取（P1①：口径统一到 text_domain，与 fidelity 产出检测单一来源）
_extract_numbers = extract_numbers


def load_argument(code: str, date: str, conn) -> RecommendationArgument | None:
    """读当日论证。优先级：recommendation_rationale（1a 结构化，fe45b0f3 契约，
    rationale_id/trace_id JOIN 锚拿全）→ 回落 agent_memory（过渡原料，schema 接通期
    rationale 表还空时 1b 仍可用）。
    conn: sqlite3.Connection（只读使用）。无行返回 None（诚实水位，调用方显式「理由生成中」）。"""
    if conn is None:
        return None
    arg = load_argument_from_rationale(code, date, conn)
    if arg is not None:
        # 数字/因子域补全（闸2 白名单面；1a claims 已带 source 域时由 rationale_join 填）
        if not arg.numbers:
            arg.numbers = _extract_numbers(arg.reasoning)
        return arg
    try:
        row = conn.execute(
            "SELECT verdict, confidence, reasoning FROM agent_memory "
            "WHERE code = ? AND date = ? ORDER BY rowid DESC LIMIT 1",
            (code, date),
        ).fetchone()
    except sqlite3.OperationalError:
        # 表不存在（全新库/未迁移）→ 视同无原料，返回 None 走诚实水位，
        # 绝不让「缺表」把整个报告节炸成「暂无推荐」（判例：空集与故障须可区分）
        return None
    if not row:
        return None
    verdict, confidence, reasoning = row
    reasoning = (reasoning or "").strip()
    if not reasoning:
        return None
    return RecommendationArgument(
        code=code,
        verdict=_VERDICT_MAP.get((verdict or "").strip(), _CONSERVATIVE_VERDICT),
        confidence=float(confidence) if confidence is not None else 0.0,
        position_pct=0.0,          # agent_memory 无仓位面（FinalDecision 才有），先置零
        reasoning=reasoning,
        factors=extract_factors(  # 件②单一入口: 1a schema 落表后 register_extractor 换实现,调用方不动
            SimpleNamespace(reasoning=reasoning, factors=None)),
        numbers=_extract_numbers(reasoning),
    )
