"""晨报水位 Section — 投递链尾全败的 outbox 滞留可见化 (P0-2 ①).

铁律#12 留痕 + 可观测不可静默: 只要 outbox 里有未清条目, 早报置顶
亮水位, 让操作者第一眼看到"有消息没发出去". 无滞留时返回 None,
该段自动隐身, 不打扰正常早报. 纯文件系统读取, 零网络, 绝不抛.
"""
from __future__ import annotations

from delivery_layer.outbox import OutboxStore
from report_layer.sections.base import Section, SectionResult
from report_layer.sections.base import note_degraded


class OutboxWatermarkSection(Section):
    name = "outbox_watermark"

    def build(self, context) -> SectionResult | None:
        try:
            entries = OutboxStore().pending()
        except Exception as _deg_exc:
            note_degraded("outbox_watermark", _deg_exc)
            return None
        if not entries:
            return None
        # 只展示最近 5 条 + 总数, 避免水位段把早报撑爆
        recent = entries[-5:]
        lines = [f"⚠️ **投递水位**: {len(entries)} 条消息链尾全败, 已落 outbox"]
        for e in recent:
            ts = (e.get("ts") or "?")[:16].replace("T", " ")
            ct = e.get("content_type", "?")
            title = e.get("title", "")
            chans = ",".join(a.get("channel", "?") for a in e.get("attempts", []))
            err = (e.get("attempts") or [{}])[-1].get("error", "")
            tail = f" | {err}" if err else ""
            lines.append(f"- `{ts}` {ct} 「{title}」 经 [{chans}]{tail}")
        if len(entries) > len(recent):
            lines.append(f"- … 其余 {len(entries) - len(recent)} 条略")
        lines.append("")
        lines.append("> 重投: 由运维人工确认后清理重投; SMTP 留位未启用")
        return SectionResult(
            title="投递水位",
            body="\n".join(lines),
            name=self.name,
            sources=["outbox"],
        )
