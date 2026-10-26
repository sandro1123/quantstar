"""多级 Guardrail — PII/合规/内容三级过滤"""
import re

from common.logging import get_logger

logger = get_logger("agent.security.guardrail")


PII_PATTERNS = [
    # AG-GUARD 20260907: 加数字边界断言 (?<![\d.]) ... (?![\d.])
    # 病案: 决策 prompt 注入全精度因子分 (如 -0.09671398819198654), 小数位
    # '13988191986' 误中手机号正则, 75/75 全拦 → LLM 研判全灭降级规则模式
    # (日志 09-01~09-04 五批 375 次实证)。边界断言: 匹配串前后不得是数字/
    # 小数点 → 浮点片段放行, 真隐私串 (前后为文字/标点) 照拦不误。
    (r"(?<![\d.])\d{17}[\dXx](?![\d.])", "身份证号"),       # 18位身份证
    (r"(?<![\d.])1[3-9]\d{9}(?![\d.])", "手机号"),          # 11位手机号
]

COMPLIANCE_PATTERNS = [
    ("内幕交易", "内幕交易"),
    ("操纵市场", "市场操纵"),
    ("保证收益", "保证收益"),
    ("稳赚", "稳赚承诺"),
    ("内幕", "内幕信息"),
]

CONTENT_PATTERNS = [
    (r"推荐.*满仓", "满仓推荐"),
    (r"保证.*收益", "收益保证"),
    (r"肯定.*涨", "肯定上涨"),
    (r"100%.*机会", "百分百机会"),
]


class GuardrailResult:
    def __init__(self):
        self.flagged: list[tuple[str, str, str]] = []  # (level, type, detail)
        self.passed: bool = True

    def add(self, level: str, gtype: str, detail: str):
        self.flagged.append((level, gtype, detail))
        self.passed = False


class Guardrail:
    LEVELS = ["pii", "compliance", "content"]

    def check(self, text: str) -> GuardrailResult:
        result = GuardrailResult()

        for pattern, desc in PII_PATTERNS:
            if re.search(pattern, text):
                result.add("pii", desc, f"PII模式匹配: {pattern}")

        for keyword, desc in COMPLIANCE_PATTERNS:
            if keyword in text:
                result.add("compliance", desc, f"合规关键词: {keyword}")

        for pattern, desc in CONTENT_PATTERNS:
            if re.search(pattern, text):
                result.add("content", desc, f"内容红线: {pattern}")

        if not result.passed:
            logger.warning(f"Guardrail blocked: {result.flagged}")
        return result
