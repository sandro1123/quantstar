# -*- coding: utf-8 -*-
"""件② 因子面提取单一入口 — extract_factors（可替换实现，调用方零改动）

过渡态（现在）：factors 从 reasoning 文本提取（_extract_factors_text）。
目标态（1a schema 落表后）：推荐论证自带结构化因子列表 → 直接读字段。
换法：register_extractor(new_fn) 一处，调用方（source.py）不动。

统筹裁定边界：只做到接口预留，不提前写死 1a schema 字段（schema 未定）。
"""
from common.logging import get_logger
import re

logger = get_logger("report.advisor.factor_extract")

_FACTOR_RE = re.compile(r"([一-龥A-Za-z_]+?因子)")

# 当前注册的提取器（None=默认文本提取）。跨测试单例污染防线：测试用 register_extractor(None) 还原。
_custom_extractor = None


def _extract_factors_text(reasoning: str) -> list:
    """过渡态实现：文本提取（'动量因子'→'动量'）。"""
    if not reasoning:
        return []
    seen, out = set(), []
    for m in _FACTOR_RE.finditer(reasoning):
        name = m.group(1)
        core = name[:-2] if name.endswith("因子") else name
        if core and core not in seen:
            seen.add(core)
            out.append(core)
    return out


def register_extractor(fn) -> None:
    """注册自定义因子提取器（1a schema 到位时换实现的一处口）。None=还原默认。"""
    global _custom_extractor
    _custom_extractor = fn


def extract_factors(arg) -> list:
    """单一入口：原料论证 → 因子名列表。
    优先原料自带结构化 factors 字段（1a 落表后的常态），
    其次自定义提取器，兜底文本提取（过渡态）。提取器抛错回落文本提取不炸主链。"""
    if arg is None:
        return []
    # 1a 落表后的常态路径：原料自带结构化字段直接用
    structured = getattr(arg, "factors", None)
    if structured:
        return list(structured)
    if _custom_extractor is not None:
        try:
            out = _custom_extractor(arg)
            if out is not None:
                return list(out)
        except Exception as e:
            logger.warning(f"自定义因子提取器失败,回落文本提取: {e}")
    return _extract_factors_text(getattr(arg, "reasoning", "") or "")
