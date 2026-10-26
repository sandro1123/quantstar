# -*- coding: utf-8 -*-
"""运维归档通道 (第四波 宪法 #12 施工面)。

运维件 (ops_report/dev_verify) 从用户 DM 直推改归档+日报聚合:
- send() = 写 delivery_outbox 归档记录 (复用 P0-2 ① 落地仓同形态
  文件面, 归档目录 delivery_archive/), 零网络零 DM;
- 下游消费: 晨报 outbox_watermark 段读归档面聚合展示 (水位面已有)。
"""
from __future__ import annotations

import json
import os
import time

from delivery_layer.channels.base import Channel, DeliveryResult
from delivery_layer.message import Message


class OpsArchiveChannel(Channel):
    """运维归档: 写 JSONL 归档, 零网络。"""

    @property
    def name(self) -> str:
        return "ops_archive"

    def _archive_path(self) -> str:
        root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))))
        d = os.path.join(root, "logs", "delivery_archive")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "ops_%s.jsonl" % time.strftime("%Y%m%d"))

    def send(self, message: Message) -> DeliveryResult:
        try:
            rec = {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "content_type": message.content_type,
                "title": message.title,
                "severity": message.severity,
                "message_id": message.message_id,
                "content": message.content if isinstance(
                    message.content, (dict, list)) else str(message.content),
            }
            with open(self._archive_path(), "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            return DeliveryResult(message_id=message.message_id,
                                  channel=self.name, status="success")
        except Exception as e:  # noqa: BLE001
            return DeliveryResult(message_id=message.message_id,
                                  channel=self.name, status="failed",
                                  error_msg=str(e))

    def health(self) -> bool:
        """归档通道健康面: 归档目录可写即健康 (本地文件面无外部依赖)。"""
        try:
            path = self._archive_path()
            return os.access(os.path.dirname(path), os.W_OK)
        except Exception:  # noqa: BLE001
            return False

