# -*- coding: utf-8 -*-
"""文本域提取单一来源 — 数字/因子 regex 常量（审计 P1① 修复）

审计复核 P1（2026-09-21）：source.py 与 fidelity.py 各持一套数字 regex 口径
（source: [%元日]/[成折年月日]；fidelity: [%元]/[成折]），双口径 = 白名单两侧不等宽
→ 闸2 可能因口径差漏判或误判。修法：两处共引本模块常量，单一来源。
"""
import re

# 原料数字域提取（source.py 侧）+ 产出数字检测（fidelity.py 侧）共用同一口径。
# 说明：中文量词类含「日/月/年」（"近20日"/"三年"）与「成/折」（"五成"），
# ASCII 类含 %/元/日（"10.5元"/"20日"）—— 两侧必须逐字相同。
NUM_PATTERN = r"\d+(?:\.\d+)?[%元日]?|[一二三四五六七八九十百]+[成折年月日]"
_NUM_RE = re.compile(NUM_PATTERN)

# 因子名提取（"动量因子"→core "动量"）
FACTOR_PATTERN = r"([一-龥A-Za-z_]+?因子)"
_FACTOR_RE = re.compile(FACTOR_PATTERN)


def extract_numbers(text: str) -> list:
    """按统一口径提取数字（去重保序）"""
    if not text:
        return []
    seen, out = set(), []
    for m in _NUM_RE.findall(text):
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return out


def find_numbers(text: str) -> list:
    """按统一口径查找数字（保序不去重，供产出侧检测）"""
    return _NUM_RE.findall(text or "")


def find_factors(text: str) -> list:
    """查找因子名（原始匹配串，如 '动量因子'）"""
    return _FACTOR_RE.finditer(text or "")
