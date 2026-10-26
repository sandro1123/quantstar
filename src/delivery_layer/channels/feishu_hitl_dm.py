"""HITL 专用私聊通道 — 新 app 凭据 + union_id 收件人 (Phase 2 app 分离)

为什么必须独立通道: 卡片归属哪个 app 发出, 按钮回调就投给哪个 app 的长连接。
HITL 卡片若走旧 app (feishu_dm) 发出, 回调投给 hermes 那条连接, daemon 切到
新 app 后一张回调都收不到 —— 比现状还糟。故 HITL 卡片 (审批/通知/结果) 锁死
从新 app 发出。晨报/告警等其它 DM 流量仍走 feishu_dm (旧 app), 不受影响。

收件人用 union_id: open_id 是 app 作用域, FEISHU_DM_ID (oc_***) 是旧 app 的会话
id, 新 app 不能复用; union_id 是租户作用域跨 app 不变, 发消息 API 支持
receive_id_type=union_id。解析脚本已把用户 union_id 落 FEISHU_HITL_DM_UNION_ID。

凭据回退语义与 daemon 切换点 A 一致: 新键缺失时回退共用键 (回滚=删 .env 新键)。
"""

import json
import os
import time

import requests

from common.logging import get_logger
from config_layer import Config
from delivery_layer.channels.base import Channel, DeliveryResult
from delivery_layer.channels.feishu_dm import FEISHU_API_BASE, _get_cached_token
from delivery_layer.message import Message

logger = get_logger("delivery.feishu_hitl_dm")


class FeishuHitlDMChannel(Channel):
    def __init__(self, config: Config | None = None):
        self.config = config

    def _env(self, key: str, default: str = "") -> str:
        if self.config:
            val = self.config.get(f"delivery.feishu.{key.lower()}")
            if val:
                return str(val)
        return os.getenv(key, default)

    @property
    def name(self) -> str:
        return "feishu_hitl_dm"

    def _creds(self) -> tuple[str, str]:
        """新 app 凭据优先, 缺则回退共用键 (与 daemon 切换点 A 同语义)"""
        app_id = self._env("FEISHU_HITL_APP_ID") or self._env("FEISHU_APP_ID")
        app_secret = self._env("FEISHU_HITL_APP_SECRET") or self._env("FEISHU_APP_SECRET")
        return app_id, app_secret

    def _recipient(self) -> tuple[str, str]:
        """union_id 优先 (新 app 作用域收件人), 缺则回退旧 chat_id。返回 (id, id_type)"""
        union_id = self._env("FEISHU_HITL_DM_UNION_ID")
        if union_id:
            return union_id, "union_id"
        dm_id = self._env("FEISHU_DM_ID")
        return dm_id, "chat_id"

    def send(self, message: Message) -> DeliveryResult:
        start = time.time()
        app_id, app_secret = self._creds()
        receive_id, id_type = self._recipient()
        if not (app_id and app_secret and receive_id):
            return DeliveryResult(
                message_id=message.message_id, channel=self.name, status="failed",
                error_msg="HITL 凭据或收件人缺失",
                duration_ms=int((time.time() - start) * 1000))

        token = _get_cached_token(app_id, app_secret)
        if not token:
            return DeliveryResult(
                message_id=message.message_id, channel=self.name, status="failed",
                error_msg="token 获取失败",
                duration_ms=int((time.time() - start) * 1000))

        content = message.content
        msg_type = content.get("msg_type", "text")
        if msg_type == "interactive":
            body = json.dumps(content.get("card", content), ensure_ascii=False)
        elif msg_type == "text":
            body = json.dumps(content.get("content", content), ensure_ascii=False)
        else:
            body = json.dumps(content, ensure_ascii=False)

        payload = {"receive_id": receive_id, "msg_type": msg_type, "content": body}
        try:
            resp = requests.post(
                f"{FEISHU_API_BASE}/im/v1/messages?receive_id_type={id_type}",
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
                json=payload, timeout=30)
            data = resp.json()
            if data.get("code") == 0:
                remote_id = (data.get("data") or {}).get("message_id", "")
                return DeliveryResult(
                    message_id=message.message_id, channel=self.name, status="success",
                    duration_ms=int((time.time() - start) * 1000),
                    remote_message_id=remote_id)
            error_msg = data.get("msg", f"code={data.get('code')}")
            logger.warning(f"HITL DM 推送失败: {error_msg}")
            return DeliveryResult(
                message_id=message.message_id, channel=self.name, status="failed",
                error_msg=error_msg, duration_ms=int((time.time() - start) * 1000))
        except Exception as e:
            logger.error(f"HITL DM 推送异常: {e}")
            return DeliveryResult(
                message_id=message.message_id, channel=self.name, status="failed",
                error_msg=str(e), duration_ms=int((time.time() - start) * 1000))

    def health(self) -> bool:
        app_id, app_secret = self._creds()
        receive_id, _ = self._recipient()
        return bool(app_id and app_secret and receive_id)
