# -*- coding: utf-8 -*-
"""请求字段包白名单 (AG-QUOTA-2 件2: token 按 agent 裁剪瘦身)。

按 task 类型裁 input 字段包: 技术类 (market_analysis/intraday) 不吃
财务三表字段; 基本面类 (stock_analysis) 保留核心+财务。白名单制:
不匹配 task → 全字段包 (宁可多给不静默缺)。
"""
from __future__ import annotations

# 字段包: input 键白名单 per task (None=全字段包)
_TASK_FIELDS: dict[str, list[str] | None] = {
    # 技术面类: 量价/波动/情绪 — 不吃财务三表
    "market_analysis": ["code", "date", "drop_pct", "type", "change_pct",
                        "volume", "turnover_ratio", "market_regime"],
    "entry_check": ["code", "entry_price", "close", "volume",
                    "turnover_ratio", "factor_scores", "composite_score"],
    "exit_review": ["code", "exit_price", "entry_price", "close",
                    "pnl_pct", "hold_days", "exit_reason"],
    "quick_scan": ["code", "date", "factor_scores", "composite_score"],
    # 基本面综合类: 核心+财务三表 (合理保留)
    "stock_analysis": ["code", "date", "factor_scores", "composite_score",
                       "news_summary", "financial", "income_statement",
                       "balance_sheet", "cash_flow", "close", "volume",
                       "turnover_ratio", "drop_pct", "type"],
}

# 深审链字段包 (deep_review): 综合全字段 (六专家各取所需)
_TASK_FIELDS["deep_review"] = None


def profile_for(task: str) -> dict:
    """task → 字段包 profile (fields=None 表示全字段包)。"""
    return {"fields": _TASK_FIELDS.get(task)}


def trim_input(task: str, input_data: dict) -> dict:
    """按白名单裁剪 input; task 无白名单 → 原样返回 (禁静默缺数据)。"""
    fields = _TASK_FIELDS.get(task)
    if fields is None:
        return input_data
    return {k: v for k, v in input_data.items() if k in fields}
