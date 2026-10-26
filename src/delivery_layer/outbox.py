"""Outbox 落地仓 — 投递链尾全败的最后兜底 (P0-2 failover ①).

统筹裁定 (2026-08-27): ①outbox 文件落地=批准施工; ②SMTP 留位不造;
③备用飞书群=否决 (死通道风险).

DeliveryExecutor 链尾全败 (或无健康通道/无路由) 时, 消息追加落
data/outbox/outbox-YYYYMMDD.jsonl. 纯文件系统, 零网络, 任何异常
返回 None/[], 绝不抛出, 绝不阻塞投递主链. 晨报水位
(OutboxWatermarkSection) 扫描本模块未清条目展示 (铁律#12 留痕).

记录格式 (每行一个 JSON):
    {ts, message_id, content_type, title, severity, source,
     route, attempts: [{channel, status, error, retry_count}], payload}

清理语义: mark_cleared 把 message_id 追加进 cleared.jsonl (运维
手工入口), pending() 汇总时排除. 本期不做自动重投 —— 只负责落地
与可见, 重投由人决定. SMTP 留位 (②): 未来若接邮件通道, 消费
pending() 重投即可, 本模块接口不变.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from delivery_layer.channels.base import DeliveryResult
    from delivery_layer.message import Message

REPO_ROOT = Path(__file__).resolve().parents[2]


def default_outbox_dir() -> Path:
    """data/outbox (QS_OUTBOX_DIR 可覆盖, 调用时读取便于测试隔离)."""
    override = os.environ.get("QS_OUTBOX_DIR")
    return Path(override) if override else REPO_ROOT / "data" / "outbox"


class OutboxStore:
    def __init__(self, outbox_dir: str | Path | None = None):
        self.dir = Path(outbox_dir) if outbox_dir else default_outbox_dir()

    def _daily_file(self, ts: datetime) -> Path:
        return self.dir / f"outbox-{ts.strftime('%Y%m%d')}.jsonl"

    def land(self, message: Message, route: list[str],
             results: list[DeliveryResult]) -> Path | None:
        """追加落地一条全败消息. 返回落地路径; 失败返回 None (绝不抛)."""
        try:
            now = datetime.now()
            entry = {
                "ts": now.isoformat(timespec="seconds"),
                "message_id": message.message_id,
                "content_type": message.content_type,
                "title": message.title,
                "severity": message.severity,
                "source": message.source,
                "route": list(route or []),
                "attempts": [
                    {
                        "channel": r.channel,
                        "status": r.status,
                        "error": r.error_msg or "",
                        "retry_count": r.retry_count,
                    }
                    for r in (results or [])
                ],
                "payload": message.content,
            }
            self.dir.mkdir(parents=True, exist_ok=True)
            path = self._daily_file(now)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
            return path
        except Exception:
            return None

    def pending(self) -> list[dict]:
        """全部未清条目 (跨每日文件, 按 ts 升序). 目录缺失/异常返回 []."""
        try:
            if not self.dir.is_dir():
                return []
            cleared = self._cleared_ids()
            entries = []
            for path in sorted(self.dir.glob("outbox-*.jsonl")):
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if entry.get("message_id") in cleared:
                        continue
                    entries.append(entry)
            entries.sort(key=lambda e: e.get("ts", ""))
            return entries
        except Exception:
            return []

    def mark_cleared(self, message_ids: list[str]) -> int:
        """清理入口: message_ids 追加进 cleared.jsonl. 返回写入条数."""
        try:
            if not message_ids:
                return 0
            self.dir.mkdir(parents=True, exist_ok=True)
            now = datetime.now().isoformat(timespec="seconds")
            path = self.dir / "cleared.jsonl"
            with open(path, "a", encoding="utf-8") as f:
                f.writelines(json.dumps({"message_id": mid, "cleared_at": now},
                                       ensure_ascii=False) + "\n" for mid in message_ids)
            return len(message_ids)
        except Exception:
            return 0

    def _cleared_ids(self) -> set[str]:
        path = self.dir / "cleared.jsonl"
        if not path.exists():
            return set()
        ids: set[str] = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ids.add(json.loads(line).get("message_id"))
            except ValueError:
                continue
        return ids
