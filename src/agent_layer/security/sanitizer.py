"""InputSanitizer — prompt injection 防御"""
import re
import unicodedata

from common.logging import get_logger

logger = get_logger("agent.security")

INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?instructions",
    r"ignore\s+(all\s+)?previous",
    r"ignore\s+(all\s+)?above",
    r"system\s*prompt",
    r"you\s+are\s+now",
    r"new\s+instructions",
    r"override\s+(all\s+|the\s+|your\s+|any\s+)?(previous|prior|system|instructions?|rules?)",
    r"jailbreak",
    r"DAN\s+mode",
    r"act\s+as\s+if",
    r"disregard\s+(all\s+)?(previous|above|prior)",
    # 中文注入句式 (新闻/公告/网页文本最常见的攻击载体)
    r"忽略(以上|上面|之前|前面|先前|所有|全部)?(的)?(所有|全部)?(指令|指示|提示|规则|要求|设定)",
    r"无视(以上|上面|之前|前面|所有)?(的)?(指令|指示|提示|规则|要求)",
    r"(系统|system)\s*提示(词)?",
    r"你现在(是|扮演|充当)",
    r"(从现在起|从现在开始)(你|请)",
    r"新的?(指令|指示)[:：]",
    r"(扮演|假装你是)",
    r"(必须|务必)(给出|输出)(满分|最高分|强烈买入|score\s*=?\s*1)",
]

MAX_DEPTH = 6

# 零宽/不可见字符: 攻击者插在关键词中间绕过正则
_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"), None)


def _normalize(text: str) -> str:
    """NFKC 归一化 (全角→半角等) + 去零宽字符, 再做注入检测。"""
    return unicodedata.normalize("NFKC", text).translate(_ZERO_WIDTH)


class InputSanitizer:
    def __init__(self):
        self._patterns = [re.compile(p, re.IGNORECASE) for p in INJECTION_PATTERNS]

    def sanitize(self, data: dict) -> dict:
        """递归清洗 dict / list / tuple 中的所有字符串 (此前只处理最外层字符串)。"""
        return {key: self._walk(value, str(key), 0) for key, value in data.items()}

    def sanitize_text(self, text: str, field: str = "text") -> str:
        return self._walk(text, field, 0)

    def _walk(self, value, path: str, depth: int):
        if depth > MAX_DEPTH:
            return value
        if isinstance(value, str):
            normalized = _normalize(value)
            if self._detect_injection(normalized):
                logger.warning(f"injection detected in field: {path}")
                return self._clean(normalized)
            return value
        if isinstance(value, dict):
            return {k: self._walk(v, f"{path}.{k}", depth + 1) for k, v in value.items()}
        if isinstance(value, list):
            return [self._walk(v, f"{path}[{i}]", depth + 1) for i, v in enumerate(value)]
        if isinstance(value, tuple):
            return tuple(self._walk(v, f"{path}[{i}]", depth + 1) for i, v in enumerate(value))
        return value

    def _detect_injection(self, text: str) -> bool:
        return any(p.search(text) for p in self._patterns)

    def _clean(self, text: str) -> str:
        for p in self._patterns:
            text = p.sub("", text)
        return text.strip()
