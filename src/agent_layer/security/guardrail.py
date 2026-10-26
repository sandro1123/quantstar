"""多级 Guardrail — PII/合规/内容三级过滤

输入端 (check_input) 只拦 PII: 新闻/公告里出现「内幕交易立案」「操纵市场」恰恰是
风控专家最需要看到的风险信号, 不能因关键词整条丢掉 (2026-10-26 审计 P0-4)。
合规/内容红线 (承诺收益、满仓推荐等) 作用在模型输出端 (check_output)。
"""
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

# 仅用于模型输出: 系统自己不得做出的表述 (承诺收益/声称掌握内幕/喊满仓)。
COMPLIANCE_PATTERNS = [
    (r"保证收益", "保证收益"),
    (r"稳赚", "稳赚承诺"),
    (r"(有|掌握|获得|拿到|提供)(了)?内幕(消息|信息)", "内幕信息"),
    (r"(配合|一起|帮你)(拉升|操纵)", "市场操纵"),
]

CONTENT_PATTERNS = [
    (r"推荐.{0,6}满仓", "满仓推荐"),
    (r"保证.{0,6}收益", "收益保证"),
    (r"肯定(会)?(大)?涨(?!跌)", "肯定上涨"),
    (r"100%.{0,6}(机会|上涨|盈利|赚)", "百分百机会"),
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

    def check_input(self, text: str) -> GuardrailResult:
        """输入端: 只做 PII 检查, 不因风险关键词丢弃数据。"""
        result = GuardrailResult()
        self._check_pii(text, result)
        if not result.passed:
            logger.warning(f"Guardrail blocked input: {result.flagged}")
        return result

    def check_output(self, text: str) -> GuardrailResult:
        return self.check(text)

    @staticmethod
    def _check_pii(text: str, result: GuardrailResult) -> None:
        for pattern, desc in PII_PATTERNS:
            if re.search(pattern, text):
                result.add("pii", desc, f"PII模式匹配: {pattern}")

    def check(self, text: str) -> GuardrailResult:
        """完整检查 (PII + 合规 + 内容), 用于模型输出。"""
        result = GuardrailResult()
        self._check_pii(text, result)

        for pattern, desc in COMPLIANCE_PATTERNS:
            if re.search(pattern, text):
                result.add("compliance", desc, f"合规红线: {pattern}")

        for pattern, desc in CONTENT_PATTERNS:
            if re.search(pattern, text):
                result.add("content", desc, f"内容红线: {pattern}")

        if not result.passed:
            logger.warning(f"Guardrail blocked: {result.flagged}")
        return result
