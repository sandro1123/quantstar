"""OutputValidator — 幻觉检测 + 数值合理性校验"""
from common.logging import get_logger

logger = get_logger("agent.security")


class OutputValidator:
    def validate(self, output: dict) -> tuple[bool, list[str]]:
        warnings = []
        if "pe" in output:
            pe = output["pe"]
            if isinstance(pe, (int, float)) and (pe > 1000 or pe < 0):
                warnings.append(f"PE={pe} suspicious (out of A-share range)")
        if "price" in output:
            price = output["price"]
            if isinstance(price, (int, float)) and (price > 9999 or price < 0.01):
                warnings.append(f"Price={price} suspicious (out of A-share range)")
        if "verdict" in output and "confidence" in output:
            verdict = output["verdict"]
            confidence = output["confidence"]
            if verdict == "推荐" and isinstance(confidence, (int, float)) and confidence < 0.3:
                warnings.append(f"High verdict with low confidence: {verdict}/{confidence}")
        return len(warnings) == 0, warnings
