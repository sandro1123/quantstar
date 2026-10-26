"""内容校验 — 推送前检查"""

from dataclasses import dataclass

MAX_BODY_LENGTH = 5000
MAX_SECTIONS = 30


@dataclass
class ValidationIssue:
    field: str
    message: str
    severity: str = "warning"


class ContentValidator:
    def validate(self, content: dict) -> list[ValidationIssue]:
        issues = []
        sections = content.get("sections", [])
        if not sections:
            # 卡片形态 (msg_type=interactive / 含 card) 无 sections 段 — 形态不同非内容空,
            # 空判只适用于 legacy sections 形态 (2026-09-11: 晚报宪法六段出卡片,
            # 旧校验器每日误报「报告段落为空」— 校验面须跟上格式演进)。
            if not (isinstance(content, dict)
                    and ("card" in content or content.get("msg_type") == "interactive")):
                issues.append(ValidationIssue("sections", "报告段落为空", "warning"))
            return issues
        if len(sections) > MAX_SECTIONS:
            issues.append(ValidationIssue("sections", f"段落数{len(sections)}超过限制{MAX_SECTIONS}", "warning"))
        for section in sections:
            if "error" in section:
                issues.append(ValidationIssue(section.get("name", ""), section["error"], "warning"))
            body = section.get("body", "") or ""
            if isinstance(body, str) and len(body) > MAX_BODY_LENGTH:
                issues.append(ValidationIssue(section.get("name", ""),
                    f"body超长: {len(body)}字符(上限{MAX_BODY_LENGTH})", "warning"))
            if isinstance(body, str) and not body.strip():
                issues.append(ValidationIssue(section.get("name", ""), "body为空", "warning"))
        return issues
