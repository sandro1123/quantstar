"""飞书卡片格式化 — 支持 text_size/column_set/header.subtitle/markdown表格

可读性契约 (fix/report-readability):
- 标题带数据日期; header.subtitle = 一句话结论 (今日动作=…) + 合规免责句, 卡片头置顶。
- 头部颜色: 红只留 critical; info→blue / warning→yellow。
- 卡片正文不渲染「来源:」尾注 (内部表名/管道名不进用户视野)。
"""
import re

from common.logging import get_logger
from delivery_layer.message import Message

logger = get_logger("report.formatter")

DISCLAIMER = "本系统为模拟盘研究性质，非投资建议"


class FeishuCardFormatter:
    def format(self, message: Message) -> dict:
        sections = message.content.get("sections", [])
        date = message.content.get("date", "")
        header_text = message.title or "QuantStar 报告"
        if date and date not in header_text:
            header_text = f"{header_text}｜{date}"
        return {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {
                    "title": {"tag": "plain_text", "content": header_text},
                    "template": self._severity_color(message.severity),
                    "subtitle": {
                        "tag": "plain_text",
                        "content": self._headline(sections),
                    },
                },
                "elements": self._build_elements(sections),
            },
        }

    def _severity_color(self, severity: str) -> str:
        # 头部颜色契约: 红只留 critical; 日常 info 不再整卡标红
        return {"info": "blue", "warning": "yellow", "critical": "red"}.get(
            severity, "blue")

    def _headline(self, sections: list) -> str:
        """一句话结论 (今日动作=…) + 合规免责句。"""
        action = ""
        for s in sections:
            body = s.get("body", "") or ""
            if "今日动作=" in body:
                action = body.split("今日动作=", 1)[1].split("\n", 1)[0].strip(" ，。")
                break
            if s.get("name") == "tomorrow_picks":
                m = re.search(r"明日推荐（(\d+) 只）", body)
                if m:
                    action = f"可关注 {m.group(1)} 只"
                elif "暂无推荐" in body:
                    action = "只看不动"
                if action:
                    break
        parts = [f"今日动作={action}"] if action else []
        parts.append(DISCLAIMER)
        return "｜".join(parts)

    def _build_elements(self, sections: list) -> list:
        elements = []
        for i, section in enumerate(sections):
            if "error" in section:
                elements.append({
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": f"⚠️ {section.get('name','')} 生成失败: {section['error']}"},
                })
                continue
            title = section.get("title", "")
            subtitle = section.get("subtitle", "")
            body = section.get("body", "")
            columns = section.get("columns")

            if not title and body:
                elements.append({
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": body, "text_size": "notation"},
                })
                continue

            if title:
                elements.append({
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": title, "text_size": "heading-3"},
                })

            if subtitle:
                elements.append({
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": f"<font color='grey'>{subtitle}</font>", "text_size": "notation"},
                })

            if columns:
                elements.append(self._build_column_set(columns))
            elif body:
                elements.append({
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": body, "text_size": "normal"},
                })

            if i < len(sections) - 1:
                elements.append({"tag": "hr"})

        return elements

    def _build_column_set(self, columns: list) -> dict:
        """Build a column_set element from a list of column definitions.
        
        columns: [{"items": [{"text": str, "color": str}], "width": int}, ...]
        """
        col_elements = []
        for col in columns:
            weight = col.get("width", 1)
            items = col.get("items", [])
            inner = []
            for item in items:
                text = item.get("text", "")
                color = item.get("color")
                size = item.get("size", "normal")
                if color:
                    text = f"<font color='{color}'>{text}</font>"
                inner.append({
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": text, "text_size": size},
                })
            col_elements.append({
                "tag": "column",
                "width": "weighted",
                "weight": weight,
                "vertical_align": "center",
                "elements": inner,
            })
        return {
            "tag": "column_set",
            "flex_mode": "none",
            "horizontal_spacing": "default",
            "columns": col_elements,
        }
