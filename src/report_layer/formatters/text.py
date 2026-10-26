"""Simple text formatter — fallback when Feishu is unavailable"""
from delivery_layer.message import Message


class TextFormatter:
    def format(self, message: Message) -> str:
        sections = message.content.get("sections", [])
        lines = [f"=== {message.title} ==="]
        for s in sections:
            if "error" in s:
                lines.append(f"⚠️ {s.get('name','')} 生成失败: {s['error']}")
                continue
            title = s.get("title", "")
            body = s.get("body", "")
            if title:
                lines.append(f"\n{title}")

            if body:
                lines.append(body)

            # F5.4: 来源标注 (非空时尾部小字)
            sources = s.get("sources") or []
            if sources:
                lines.append(f"[来源: {' · '.join(sources)}]")

        return "\n".join(lines)
