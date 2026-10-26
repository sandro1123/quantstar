# -*- coding: utf-8 -*-
"""QS 卡 readiness-announce-aware-unify — D' exclusion 模块 (新建)。

src/strategy_layer/dp_exclusion.py — 财务类因子可用性判据 (D' 面, 判例 #163)。

显示面: 判据函数只读 FactorResult.source_period, 不碰因子计算。
答不出 (None) 或超阈陈旧 → 不可用 (不进 Beta 推荐) — 机器判据化
「success≠效果; 凡可验产物一律验产物」。
"""
from __future__ import annotations

from datetime import datetime

from common.logging import get_logger

logger = get_logger("strategy.dp_exclusion")

# 陈源阈 (自然日): 财报类真源披露窗合理上界 — 季末+45d 披露窗 ×2 季滞后上限。
# 超过 = 真源断供两季以上 = 假活 (R-6' 判据)。
MAX_STALE_DAYS = 200

_FINANCIAL_FACTORS = {"roe_quality", "value_discount", "growth_combined",
                      "leverage_low", "size_small_cap"}


def financial_factor_usable(result, as_of: str | None = None,
                            max_stale_days: int = MAX_STALE_DAYS) -> bool:
    """判据函数: 财务类因子能否进推荐 (D' 面)。

    result: FactorResult (鸭子类型 — 读 .source_period/.factor_name)
    as_of:  判定时点 (默认今日 BJT) — 测试可注入
    返回 False = 不进推荐 (exclusion); True = 可用。

    判定:
      1. 非财务类因子 → True (本判据只管财务类)
      2. source_period 为 None → False (答不出 = 假活, 判例 #163)
      3. source_period 距 as_of > max_stale_days → False (陈源装当期)
    """
    if result.factor_name not in _FINANCIAL_FACTORS:
        return True
    sp = getattr(result, "source_period", None)
    if not sp:
        logger.warning(
            "dp_exclusion: %s 无 source_period — 答不出真源期, 判不可用 (判例 #163)",
            result.factor_name)
        return False
    if as_of is None:
        from common.utils import bj_date
        as_of = bj_date()
    try:
        d = datetime.strptime(str(sp)[:10], "%Y-%m-%d").date()
        a = datetime.strptime(str(as_of)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        logger.warning(
            "dp_exclusion: %s source_period 非法 %r — 判不可用",
            result.factor_name, sp)
        return False
    stale = (a - d).days
    if stale > max_stale_days:
        logger.warning(
            "dp_exclusion: %s 真源期 %s 陈旧 %dd > %dd — 判不可用 (R-6' 假活)",
            result.factor_name, sp, stale, max_stale_days)
        return False
    return True
